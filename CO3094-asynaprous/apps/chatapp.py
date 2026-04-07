"""
apps.chatapp
~~~~~~~~~~~~~~~~~

This is the main chat application. It works as a hybrid system:

Phase 1 -- Client-Server (like signing into Messenger):
  A user logs in, the server remembers them as an "online peer",
  and anyone can ask the server "who else is online right now?"

Phase 2 -- Peer-to-Peer (like actually chatting):
  Once peers know about each other, they can send messages directly.
  The server also supports channel-based group chats (broadcast).

For authentication we support two methods the assignment requires:
  - Basic Auth (RFC 2617/7235): browser popup asking for user/pass
  - Cookies   (RFC 6265):       "remember me" via Set-Cookie header
"""

import json
import base64
import time
import os
import socket
import threading
import asyncio
import hashlib
import secrets

from daemon.asynaprous import AsynapRous

app = AsynapRous()

# ---------------------------------------------------------------
# Data storage -- everything lives in memory for simplicity.
# In a real app these would be in a database, but for this
# assignment in-memory dicts are enough to demonstrate the concepts.
# ---------------------------------------------------------------

# Hardcoded users for demo -- in production you'd hash passwords
USER_DB = {
    "admin": "admin123",
    "alice": "alice123",
    "bob": "bob123",
    "charlie": "charlie123",
}

sessions = {}       # session_token -> {username, last_seen}
peers = {}          # username -> {ip, port, peer_port, online, last_seen}
channels = {        # Pre-created channels everyone joins by default
    "General": {"members": set(), "messages": []},
    "HCMUT-BK": {"members": set(), "messages": []},
}
direct_messages = {}  # "alice:bob" (sorted) -> list of message dicts
peer_connections = {} # who is connected to whom (for P2P tracking)
notifications = {}    # username -> list of unread notification strings

# ---------------------------------------------------------------
# Helper functions used across multiple routes
# ---------------------------------------------------------------

def get_basic_auth_creds(auth_header):
    """Decodes the "Authorization: Basic <base64>" header.

    The browser encodes "user:pass" as base64 and sends it.
    We reverse that here to get the actual credentials.
    """
    if not auth_header or not auth_header.startswith('Basic '):
        return None, None
    try:
        encoded = auth_header.split(' ')[1]
        decoded = base64.b64decode(encoded).decode('utf-8')
        return decoded.split(':', 1)
    except Exception:
        return None, None


def parse_form_body(body):
    """Turns "username=alice&password=123" into {"username": "alice", ...}."""
    params = {}
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    if isinstance(body, str):
        for pair in body.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                from urllib.parse import unquote_plus
                params[unquote_plus(k)] = unquote_plus(v)
    return params


def get_session_user(headers):
    """Checks if the request has a valid session and returns the username.

    Auth is checked in this order:
      1. X-Session-Token header  (per-tab, avoids cookie collisions)
      2. Cookie session_token    (classic cookie-based fallback)
    """
    header_token = headers.get('x-session-token', '')
    if header_token:
        session = sessions.get(header_token)
        if session:
            session['last_seen'] = time.time()
            return session['username']

    cookie = headers.get('cookie', '')
    if isinstance(cookie, str):
        for pair in cookie.split(';'):
            pair = pair.strip()
            if pair.startswith('session_token='):
                token = pair.split('=', 1)[1]
                session = sessions.get(token)
                if session:
                    session['last_seen'] = time.time()
                    return session['username']
    return None


def create_session(username):
    """Generates a random token and associates it with the user.

    This token gets sent to the browser as a cookie, and the browser
    sends it back with every subsequent request so we know who they are.
    """
    token = secrets.token_hex(16)
    sessions[token] = {
        'username': username,
        'last_seen': time.time(),
    }
    return token


def register_user_online(username):
    """Marks a user as online and adds them to the default channels.

    Called right after a successful login so the user immediately
    shows up in the peer list and can see General/HCMUT-BK channels.
    """
    if username not in peers:
        peers[username] = {
            'ip': '127.0.0.1',
            'port': 0,
            'peer_port': 0,
            'online': True,
            'last_seen': time.time(),
        }
    else:
        peers[username]['online'] = True
        peers[username]['last_seen'] = time.time()

    for ch_name in channels:
        channels[ch_name]['members'].add(username)


def dm_key(user1, user2):
    """Makes a consistent storage key for DMs between two users.

    We sort the names so alice->bob and bob->alice both use "alice:bob".
    That way both users read from the same message list.
    """
    return ":".join(sorted([user1, user2]))


def add_notification(username, message):
    """Queues a notification that the user will see on their next poll."""
    if username not in notifications:
        notifications[username] = []
    notifications[username].append({
        "message": message,
        "time": time.time(),
    })


def notify_user_offline(username):
    """Logs the disconnect and notifies all other online users."""
    print("[ChatApp] Peer disconnected: {}".format(username))
    now = time.time()
    for uname, info in peers.items():
        if uname != username and info.get('online', False) and (now - info.get('last_seen', 0) < 120):
            add_notification(uname, "{} went offline".format(username))


def check_stale_peers():
    """Marks peers as offline if they missed heartbeats for over 120s."""
    now = time.time()
    for username, info in list(peers.items()):
        if info.get('online', False) and (now - info.get('last_seen', 0) >= 120):
            info['online'] = False
            notify_user_offline(username)


async def forward_to_peer(target_username, data):
    """Tries to deliver a message directly to another peer's server.

    This is the P2P part: instead of just storing the message and waiting
    for the other peer to poll, we open a TCP connection to their server
    and push the message there via asyncio (non-blocking).
    If their server is down or unreachable, the message is still stored
    centrally as a fallback.
    """
    peer_info = peers.get(target_username)
    if not peer_info or not peer_info.get('online'):
        return False

    peer_ip = peer_info.get('ip', '127.0.0.1')
    peer_port = peer_info.get('peer_port', 0)

    if not peer_port:
        return False

    try:
        payload = json.dumps(data)
        request = (
            "POST /receive-message HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        ).format(peer_ip, peer_port, len(payload.encode()), payload)

        reader, writer = await asyncio.open_connection(peer_ip, int(peer_port))
        writer.write(request.encode())
        await writer.drain()
        writer.close()
        return True
    except Exception as e:
        print("[ChatApp] P2P forward to {} failed: {}".format(target_username, e))
        return False

# ---------------------------------------------------------------
# Page serving -- these routes return actual HTML pages
# ---------------------------------------------------------------

@app.route('/index.html', methods=['GET'])
def serve_index(headers, body):
    """Root page: if you're logged in go to chat, otherwise show login form."""
    user = get_session_user(headers)
    if user:
        return "", 302, {"Location": "/chat.html"}

    try:
        with open("www/login.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {}
    except Exception:
        return "", 302, {"Location": "/login"}

@app.route('/chat.html', methods=['GET'])
def serve_chat(headers, body):
    """The main chat UI. Kicks you back to login if you're not authenticated."""
    user = get_session_user(headers)
    if not user:
        return "", 302, {"Location": "/index.html"}

    try:
        with open("www/chat.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {}
    except Exception:
        return {"error": "Chat page not found"}, 404, {}

# ---------------------------------------------------------------
# Authentication -- two methods as required by the assignment:
#   1) Basic Auth at /admin  (RFC 7235 -- browser popup)
#   2) Cookie login at /login (RFC 6265 -- form-based)
# ---------------------------------------------------------------

@app.route('/admin', methods=['GET'])
def admin_route(headers, body):
    """Basic Auth login (RFC 7235).

    When you visit /admin, the browser gets a 401 with a
    "WWW-Authenticate: Basic" header, which makes it show the
    built-in username/password popup. If credentials are correct,
    we create a session cookie and redirect to the chat page.
    """
    auth_header = headers.get('authorization', '')
    user, pw = get_basic_auth_creds(auth_header)

    if user and USER_DB.get(user) == pw:
        token = create_session(user)
        register_user_online(user)
        return "", 302, {
            "Location": "/chat.html",
            "Set-Cookie": "session_token={}; Path=/; HttpOnly".format(token),
        }

    return "Unauthorized", 401, {
        "WWW-Authenticate": 'Basic realm="HCMUT Admin Area"',
    }

@app.route('/login', methods=['GET'])
def login_page(headers, body):
    """Shows the login form. If you're already logged in, skip to chat."""
    user = get_session_user(headers)
    if user:
        return "", 302, {"Location": "/chat.html"}

    try:
        with open("www/login.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {}
    except Exception:
        return {"error": "Login page not found"}, 404, {}

@app.route('/login', methods=['POST'])
def login(headers, body):
    """Handles the login form submission (RFC 6265 cookies).

    If the username/password match, we generate a session token,
    send it back as a Set-Cookie header, and the browser will
    automatically include it in every future request.
    """
    content_type = headers.get('content-type', '')

    if 'application/json' in content_type:
        if isinstance(body, dict):
            params = body
        else:
            try:
                params = json.loads(body)
            except Exception:
                params = parse_form_body(body)
    else:
        params = parse_form_body(body)

    username = params.get('username', '')
    password = params.get('password', '')

    if USER_DB.get(username) == password:
        token = create_session(username)
        register_user_online(username)

        if 'application/json' in content_type:
            return {
                "status": "ok",
                "username": username,
                "token": token,
            }, 200, {"Set-Cookie": "session_token={}; Path=/".format(token)}
        else:
            return "Login successful", 302, {
                "Location": "/chat.html",
                "Set-Cookie": "session_token={}; Path=/; HttpOnly".format(token),
            }

    if 'application/json' in content_type:
        return {"error": "Invalid credentials"}, 401, {}
    else:
        return "Invalid credentials", 401, {}

@app.route('/logout', methods=['POST'])
def logout(headers, body):
    """Clears the session and marks the user as offline."""
    user = get_session_user(headers)
    if user:
        if user in peers and peers[user].get('online', False):
            peers[user]['online'] = False
            notify_user_offline(user)

        header_token = headers.get('x-session-token', '')
        if header_token:
            sessions.pop(header_token, None)

        cookie = headers.get('cookie', '')
        for pair in cookie.split(';'):
            pair = pair.strip()
            if pair.startswith('session_token='):
                token = pair.split('=', 1)[1]
                sessions.pop(token, None)

    return {"status": "ok"}, 200, {
        "Set-Cookie": "session_token=; Path=/; Max-Age=0",
    }

# ---------------------------------------------------------------
# Peer management -- the Client-Server part of the assignment.
# The server acts as a centralized tracker that knows who's online.
# ---------------------------------------------------------------

@app.route('/submit-info', methods=['POST'])
def submit_info(headers, body):
    """A peer tells the tracker "I'm at this IP and port".

    This is the first thing a peer does after logging in -- it registers
    itself so other peers can find it later via /get-list.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    peer_ip = data.get('ip', '127.0.0.1')
    peer_port = data.get('port', 0)
    peer_p2p_port = data.get('peer_port', 0)

    peers[user] = {
        'ip': peer_ip,
        'port': peer_port,
        'peer_port': peer_p2p_port,
        'online': True,
        'last_seen': time.time(),
    }

    print("[ChatApp] Peer registered: {} at {}:{}".format(user, peer_ip, peer_port))

    return {
        "status": "ok",
        "message": "Peer info registered",
        "username": user,
    }, 200, {}

@app.route('/get-list', methods=['GET'])
def get_list(headers, body):
    """Returns the list of all known peers and whether they're online.

    This is how a peer discovers who else is available to chat with.
    We also check if a peer has been silent for too long (120s) and
    mark them as offline automatically.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    now = time.time()
    peer_list = []
    for username, info in peers.items():
        is_online = info.get('online', False) and (now - info.get('last_seen', 0) < 120)
        peer_list.append({
            "username": username,
            "ip": info.get('ip', ''),
            "port": info.get('port', 0),
            "online": is_online,
        })

    return {
        "status": "ok",
        "peers": peer_list,
        "count": len(peer_list),
    }, 200, {}

@app.route('/add-list', methods=['POST'])
def add_list(headers, body):
    """Adds a user to a channel. Creates the channel if it doesn't exist yet."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    channel_name = data.get('channel', 'General')
    target_user = data.get('username', user)

    if channel_name not in channels:
        channels[channel_name] = {"members": set(), "messages": []}

    channels[channel_name]['members'].add(target_user)

    return {
        "status": "ok",
        "channel": channel_name,
        "members": list(channels[channel_name]['members']),
    }, 200, {}

# ---------------------------------------------------------------
# P2P connection setup -- this bridges client-server and P2P.
# After discovering peers via the tracker, you "connect" to them
# before starting to chat.
# ---------------------------------------------------------------

@app.route('/connect-peer', methods=['POST'])
def connect_peer(headers, body):
    """Establishes a P2P link between two peers.

    Both sides are recorded as "connected" so broadcast messages
    know who to deliver to. Think of it like adding a friend.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    target = data.get('target', '')

    if target not in peers:
        return {"error": "Peer not found"}, 404, {}

    if not peers[target].get('online'):
        return {"error": "Peer is offline"}, 400, {}

    if user not in peer_connections:
        peer_connections[user] = set()
    peer_connections[user].add(target)

    if target not in peer_connections:
        peer_connections[target] = set()
    peer_connections[target].add(user)

    return {
        "status": "ok",
        "message": "Connected to {}".format(target),
        "peer": {
            "username": target,
            "ip": peers[target].get('ip', ''),
            "port": peers[target].get('port', 0),
        }
    }, 200, {}

# ---------------------------------------------------------------
# Messaging -- the P2P part of the assignment.
# Messages go to channels (broadcast) or directly to one peer (DM).
# ---------------------------------------------------------------

@app.route('/broadcast-peer', methods=['POST'])
async def broadcast_peer(headers, body):
    """Sends a message to everyone in a channel (async/coroutine).

    The message is stored in the channel's message list, and we also
    try to push it directly to each member's server (P2P forwarding)
    using asyncio.gather for concurrent non-blocking delivery.
    Even if the P2P push fails, the message is still stored centrally
    so it shows up when the other user polls for new messages.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    channel = data.get('channel', 'General')
    message_text = data.get('message', '')

    if not message_text:
        return {"error": "Empty message"}, 400, {}

    if channel not in channels:
        return {"error": "Channel not found"}, 404, {}

    msg = {
        "sender": user,
        "message": message_text,
        "channel": channel,
        "timestamp": time.time(),
        "type": "channel",
    }

    channels[channel]['messages'].append(msg)

    forwarded_to = []
    tasks = []
    target_members = []
    for member in channels[channel]['members']:
        if member != user:
            add_notification(member, "New message in #{} from {}".format(channel, user))
            tasks.append(forward_to_peer(member, msg))
            target_members.append(member)

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for member, success in zip(target_members, results):
            if success is True:
                forwarded_to.append(member)

    return {
        "status": "ok",
        "message": msg,
        "forwarded_to": forwarded_to,
    }, 200, {}

@app.route('/send-peer', methods=['POST'])
async def send_peer(headers, body):
    """Sends a direct message to one specific peer (async/coroutine).

    This is the core P2P operation: alice sends "hi" to bob,
    we store it in direct_messages under the key "alice:bob",
    and also try to push it to bob's server in real-time
    using non-blocking asyncio TCP connection.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    target = data.get('target', '')
    message_text = data.get('message', '')

    if not target or not message_text:
        return {"error": "Missing target or message"}, 400, {}

    if target not in peers:
        return {"error": "Peer not found"}, 404, {}

    msg = {
        "sender": user,
        "target": target,
        "message": message_text,
        "timestamp": time.time(),
        "type": "direct",
    }

    key = dm_key(user, target)
    if key not in direct_messages:
        direct_messages[key] = []
    direct_messages[key].append(msg)

    add_notification(target, "New message from {}".format(user))
    await forward_to_peer(target, msg)

    return {"status": "ok", "message": msg}, 200, {}

@app.route('/receive-message', methods=['POST'])
def receive_message(headers, body):
    """Endpoint that other peer servers call to deliver messages to us.

    When alice's server sends a message to bob, it POSTs to bob's
    /receive-message. We just store it so bob sees it on his next poll.
    """
    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    msg_type = data.get('type', 'channel')

    if msg_type == 'direct':
        sender = data.get('sender', '')
        target = data.get('target', '')
        key = dm_key(sender, target)
        if key not in direct_messages:
            direct_messages[key] = []
        if data not in direct_messages[key]:
            direct_messages[key].append(data)
    elif msg_type == 'channel':
        channel = data.get('channel', '')
        if channel in channels:
            if data not in channels[channel]['messages']:
                channels[channel]['messages'].append(data)

    return {"status": "ok"}, 200, {}

# ---------------------------------------------------------------
# Channel management -- creating, listing, and fetching messages
# ---------------------------------------------------------------

@app.route('/channels', methods=['GET'])
def list_channels(headers, body):
    """Returns the channels the user is a member of, with last message preview."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    ch_list = []
    for name, info in channels.items():
        if user in info['members']:
            last_msg = info['messages'][-1] if info['messages'] else None
            ch_list.append({
                "name": name,
                "member_count": len(info['members']),
                "last_message": last_msg,
                "unread": len([n for n in notifications.get(user, [])
                              if name in n.get('message', '')]),
            })

    return {"status": "ok", "channels": ch_list}, 200, {}

@app.route('/channels', methods=['POST'])
def create_channel(headers, body):
    """Creates a new channel. The creator is automatically added as a member."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    name = data.get('name', '').strip()
    if not name:
        return {"error": "Channel name required"}, 400, {}

    if name in channels:
        return {"error": "Channel already exists"}, 400, {}

    channels[name] = {"members": {user}, "messages": []}

    return {
        "status": "ok",
        "channel": name,
        "members": [user],
    }, 200, {}

@app.route('/messages', methods=['GET'])
def get_messages(headers, body):
    """GET version of message fetching (not used by our JS client,
    but kept for API completeness).
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    # Parse query from the raw URL
    raw_url = headers.get('host', '')
    referer = headers.get('referer', '')

    # We parse query params from the body for simplicity (sent as JSON)
    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str) and body.strip():
            data = json.loads(body)
    except Exception:
        data = {}

    return {"status": "ok", "messages": [], "channel": ""}, 200, {}

@app.route('/messages', methods=['POST'])
async def get_messages_post(headers, body):
    """Fetches messages for a channel or DM, filtered by timestamp.

    The client sends {"channel": "General", "since": 1234567890} and
    we return only messages newer than that timestamp. This is how
    the polling mechanism works -- the client remembers the last
    timestamp it saw and only asks for what's new.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    channel = data.get('channel', '')
    dm_target = data.get('dm', '')
    since = data.get('since', 0)

    msgs = []
    context_name = ""

    if dm_target:
        key = dm_key(user, dm_target)
        all_msgs = direct_messages.get(key, [])
        msgs = [m for m in all_msgs if m.get('timestamp', 0) > since]
        context_name = dm_target
    elif channel:
        ch = channels.get(channel, {})
        all_msgs = ch.get('messages', [])
        msgs = [m for m in all_msgs if m.get('timestamp', 0) > since]
        context_name = channel

    return {
        "status": "ok",
        "messages": msgs[-100:],
        "channel": context_name,
    }, 200, {}

@app.route('/notifications', methods=['GET'])
def get_notifications(headers, body):
    """Returns and clears any pending notifications for this user.

    The JS client polls this every 2 seconds and shows a toast
    popup for each new notification (like "New message from bob").
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    user_notifs = notifications.pop(user, [])
    return {
        "status": "ok",
        "notifications": user_notifs,
        "count": len(user_notifs),
    }, 200, {}

@app.route('/heartbeat', methods=['POST'])
def heartbeat(headers, body):
    """The client pings this every 30s so we know it's still alive.

    If a peer stops sending heartbeats, after 120s we consider them offline.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    if user in peers:
        peers[user]['last_seen'] = time.time()
        peers[user]['online'] = True

    return {"status": "ok", "user": user}, 200, {}

@app.route('/me', methods=['GET'])
def get_me(headers, body):
    """Get current user info, including the session token for per-tab auth."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    token = headers.get('x-session-token', '')
    if not token:
        cookie = headers.get('cookie', '')
        if isinstance(cookie, str):
            for pair in cookie.split(';'):
                pair = pair.strip()
                if pair.startswith('session_token='):
                    token = pair.split('=', 1)[1]
                    break

    return {
        "status": "ok",
        "username": user,
        "token": token,
        "peer_info": peers.get(user, {}),
    }, 200, {}

@app.route('/conversations', methods=['GET'])
async def get_conversations(headers, body):
    """Returns ALL conversations (channels + DMs) in one list for the sidebar.

    This is what makes our sidebar look like Messenger -- everything in
    one feed, sorted by most recent message. Channels appear with a #
    prefix, DMs show the other person's name and online status.
    """
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    check_stale_peers()

    convos = []
    now = time.time()

    for name, info in channels.items():
        if user in info['members']:
            last_msg = info['messages'][-1] if info['messages'] else None
            convos.append({
                "id": "ch:" + name,
                "type": "channel",
                "name": name,
                "display_name": "#" + name,
                "last_message": last_msg,
                "last_time": last_msg['timestamp'] if last_msg else 0,
                "member_count": len(info['members']),
                "online": True,
            })

    seen_dm_peers = set()
    for key, msgs in direct_messages.items():
        parts = key.split(":")
        if user not in parts:
            continue
        other = parts[0] if parts[1] == user else parts[1]
        seen_dm_peers.add(other)
        last_msg = msgs[-1] if msgs else None
        peer_info = peers.get(other, {})
        is_online = peer_info.get('online', False) and \
            (now - peer_info.get('last_seen', 0) < 120)
        convos.append({
            "id": "dm:" + other,
            "type": "dm",
            "name": other,
            "display_name": other,
            "last_message": last_msg,
            "last_time": last_msg['timestamp'] if last_msg else 0,
            "member_count": 2,
            "online": is_online,
        })

    for uname, info in peers.items():
        if uname == user or uname in seen_dm_peers:
            continue
        is_online = info.get('online', False) and \
            (now - info.get('last_seen', 0) < 120)
        convos.append({
            "id": "dm:" + uname,
            "type": "dm",
            "name": uname,
            "display_name": uname,
            "last_message": None,
            "last_time": 0,
            "member_count": 2,
            "online": is_online,
        })

    convos.sort(key=lambda c: c['last_time'], reverse=True)

    return {"status": "ok", "conversations": convos}, 200, {}


def _register_trailing_slash_aliases():
    """The assignment PDF shows API paths with trailing slashes like /login/.

    We register our routes without slashes internally, but this function
    duplicates every route with a trailing slash so both forms work.
    """
    aliases = {}
    for (method, path), handler in app.routes.items():
        if not path.endswith('/') and not path.endswith('.html'):
            aliases[(method, path + '/')] = handler
    app.routes.update(aliases)


def create_chatapp(ip, port):
    """Entry point to create and run the chat application."""
    _register_trailing_slash_aliases()
    app.prepare_address(ip, port)
    app.run()
