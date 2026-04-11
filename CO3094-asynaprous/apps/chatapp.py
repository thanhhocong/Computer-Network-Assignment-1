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

ACCOUNTS_FILE = "accounts.json"


def load_accounts():
    """Load user accounts from accounts.json. Creates defaults if file is missing."""
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        default = {
            "admin": {"password": "admin123", "display_name": "Admin", "role": "admin"},
            "alice": {"password": "alice123", "display_name": "Alice", "role": "user"},
            "bob": {"password": "bob123", "display_name": "Bob", "role": "user"},
            "charlie": {"password": "charlie123", "display_name": "Charlie", "role": "user"},
        }
        save_accounts(default)
        return default


def save_accounts(accounts):
    """Persist accounts dict to accounts.json."""
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, indent=4, ensure_ascii=False)


def verify_credentials(username, password):
    """Check if username/password pair is valid."""
    accounts = load_accounts()
    user_data = accounts.get(username)
    return user_data is not None and user_data.get('password') == password


def get_user_role(username):
    """Return 'admin' or 'user' for the given username."""
    accounts = load_accounts()
    return accounts.get(username, {}).get('role', 'user')


def get_display_name(username):
    """Return the display name for a user, falling back to username."""
    accounts = load_accounts()
    return accounts.get(username, {}).get('display_name', username)

sessions = {}       # session_token -> {username, last_seen}
peers = {}          # username -> {ip, port, peer_port, online, last_seen}
servers = {
    "BK Discordmess": {
        "members": set(),
        "channels": {
            "chung": {"messages": []},
            "workspace": {"messages": []},
            "lạc-đề": {"messages": []},
        },
    },
}
DEFAULT_SERVER = "BK Discordmess"
DEFAULT_CHANNELS = ["chung", "workspace", "lạc-đề"]

direct_messages = {}  # "alice:bob" (sorted) -> list of message dicts
peer_connections = {} # who is connected to whom (for P2P tracking)
notifications = {}    # username -> list of unread notification strings


def is_host_server(headers):
    """Check if the request comes from the host server (127.0.0.1).

    Used to differentiate admin behavior: on localhost the admin gets the
    monitor dashboard, on LAN IP they chat like a normal user.
    """
    host = headers.get('host', '')
    return host.startswith('127.0.0.1') or host.startswith('localhost')

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
    """Marks a user as online and adds them to all servers."""
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

    for srv_name, srv in servers.items():
        srv['members'].add(username)


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
    """Coroutine that delivers a message directly to another peer's server.

    Uses asyncio.open_connection for non-blocking TCP I/O -- the event loop
    can serve other clients while the write is in progress.  This is the
    P2P part of the assignment: once peers know each other's addresses,
    messages are pushed directly without waiting for polling.
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
        http_request = (
            "POST /receive-message HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        ).format(peer_ip, peer_port, len(payload.encode()), payload)

        reader, writer = await asyncio.open_connection(peer_ip, int(peer_port))
        writer.write(http_request.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return True
    except Exception as e:
        print("[ChatApp] P2P forward to {} failed: {}".format(target_username, e))
        return False


async def _safe_forward(target, msg):
    """Fire-and-forget wrapper -- logs errors without crashing the event loop."""
    try:
        await forward_to_peer(target, msg)
    except Exception as e:
        print("[ChatApp] Background forward to {} error: {}".format(target, e))

# ---------------------------------------------------------------
# Page serving -- these routes return actual HTML pages
# ---------------------------------------------------------------

@app.route('/index.html', methods=['GET'])
def serve_index(headers, body):
    """Root page: if you're logged in go to chat, otherwise show login form."""
    user = get_session_user(headers)
    if user:
        if get_user_role(user) == 'admin' and is_host_server(headers):
            return "", 302, {"Location": "/admin-monitor.html"}
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

@app.route('/admin-monitor.html', methods=['GET'])
def serve_admin_monitor(headers, body):
    """Admin conversation monitor. Only accessible to admins from localhost."""
    user = get_session_user(headers)
    if not user:
        return "", 302, {"Location": "/login"}
    if get_user_role(user) != 'admin':
        return "", 302, {"Location": "/chat.html"}
    if not is_host_server(headers):
        return "", 302, {"Location": "/chat.html"}

    try:
        with open("www/admin-monitor.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {}
    except Exception:
        return {"error": "Admin monitor page not found"}, 404, {}

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

    If the admin logs in from 127.0.0.1, redirect to the admin
    monitor dashboard instead of the normal chat UI.
    """
    auth_header = headers.get('authorization', '')
    user, pw = get_basic_auth_creds(auth_header)

    if user and verify_credentials(user, pw):
        token = create_session(user)
        register_user_online(user)
        # Admin from localhost -> monitor dashboard
        redirect_to = "/chat.html"
        if get_user_role(user) == 'admin' and is_host_server(headers):
            redirect_to = "/admin-monitor.html"
        return "", 302, {
            "Location": redirect_to,
            "Set-Cookie": "session_token={}; Path=/; HttpOnly; Max-Age=604800".format(token),
        }

    return "Unauthorized", 401, {
        "WWW-Authenticate": 'Basic realm="HCMUT Admin Area"',
    }

@app.route('/login', methods=['GET'])
def login_page(headers, body):
    """Shows the login form. If you're already logged in, skip to chat."""
    user = get_session_user(headers)
    if user:
        if get_user_role(user) == 'admin' and is_host_server(headers):
            return "", 302, {"Location": "/admin-monitor.html"}
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

    if verify_credentials(username, password):
        token = create_session(username)
        register_user_online(username)

        # Determine redirect target: admin from localhost goes to monitor
        is_admin_localhost = (get_user_role(username) == 'admin' and
                             is_host_server(headers))
        redirect_target = "/admin-monitor.html" if is_admin_localhost else "/chat.html"

        if 'application/json' in content_type:
            return {
                "status": "ok",
                "username": username,
                "token": token,
                "role": get_user_role(username),
                "redirect": redirect_target,
            }, 200, {"Set-Cookie": "session_token={}; Path=/; Max-Age=604800".format(token)}
        else:
            return "Login successful", 302, {
                "Location": redirect_target,
                "Set-Cookie": "session_token={}; Path=/; HttpOnly; Max-Age=604800".format(token),
            }

    if 'application/json' in content_type:
        return {"error": "Invalid credentials"}, 401, {}
    else:
        return "Invalid credentials", 401, {}

@app.route('/signup', methods=['GET'])
def signup_page(headers, body):
    """Shows the signup form for creating a new account."""
    user = get_session_user(headers)
    if user:
        return "", 302, {"Location": "/chat.html"}

    try:
        with open("www/signup.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {}
    except Exception:
        return {"error": "Signup page not found"}, 404, {}

@app.route('/signup', methods=['POST'])
def signup(headers, body):
    """Creates a new user account and saves it to accounts.json.

    New accounts always get the 'user' role -- only pre-seeded accounts
    in accounts.json can have the 'admin' role.
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

    username = params.get('username', '').strip()
    password = params.get('password', '').strip()
    display_name = params.get('display_name', '').strip() or username

    if not username or not password:
        return {"error": "Username and password are required"}, 400, {}

    if len(username) < 3:
        return {"error": "Username must be at least 3 characters"}, 400, {}

    if len(password) < 4:
        return {"error": "Password must be at least 4 characters"}, 400, {}

    accounts = load_accounts()
    if username in accounts:
        return {"error": "Username already taken"}, 409, {}

    email = params.get('email', '').strip()
    accounts[username] = {
        "password": password,
        "display_name": display_name,
        "email": email,
        "role": "user",
    }
    save_accounts(accounts)

    print("[ChatApp] New account registered: {} ({})".format(username, display_name))
    return {"status": "ok", "message": "Account created successfully"}, 201, {}

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
            "role": get_user_role(username),
        })

    return {
        "status": "ok",
        "peers": peer_list,
        "count": len(peer_list),
    }, 200, {}

@app.route('/add-list', methods=['POST'])
def add_list(headers, body):
    """Adds a user to a server."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', DEFAULT_SERVER)
    target_user = data.get('username', user)

    srv = servers.get(server_name)
    if not srv:
        return {"error": "Server not found"}, 404, {}

    srv['members'].add(target_user)

    return {
        "status": "ok",
        "server": server_name,
        "members": list(srv['members']),
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
    """Sends a message to everyone in a server channel."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', DEFAULT_SERVER)
    channel_name = data.get('channel', '')
    message_text = data.get('message', '')

    if not message_text:
        return {"error": "Empty message"}, 400, {}

    srv = servers.get(server_name)
    if not srv:
        return {"error": "Server not found"}, 404, {}

    ch = srv['channels'].get(channel_name)
    if not ch:
        return {"error": "Channel not found"}, 404, {}

    msg = {
        "sender": user,
        "message": message_text,
        "server": server_name,
        "channel": channel_name,
        "timestamp": time.time(),
        "type": "channel",
    }

    ch['messages'].append(msg)

    forwarded_to = []
    for member in srv['members']:
        if member != user:
            add_notification(member, "New message in #{} from {}".format(channel_name, user))
            asyncio.create_task(_safe_forward(member, msg))
            forwarded_to.append(member)

    return {
        "status": "ok",
        "message": msg,
        "forwarded_to": forwarded_to,
    }, 200, {}

@app.route('/send-peer', methods=['POST'])
async def send_peer(headers, body):
    """Sends a direct message to one specific peer (coroutine).

    The message is stored centrally first so it is immediately available
    for polling, then a background asyncio task pushes it to the peer's
    server via non-blocking TCP (asyncio.open_connection).  The HTTP
    response returns without waiting for the P2P transfer to finish.
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
    asyncio.create_task(_safe_forward(target, msg))

    return {"status": "ok", "message": msg}, 200, {}

@app.route('/receive-message', methods=['POST'])
def receive_message(headers, body):
    """Endpoint that other peer servers call to deliver messages to us."""
    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    msg_type = data.get('type', 'channel')
    sender = data.get('sender', '')
    ts = data.get('timestamp', 0)

    if msg_type == 'direct':
        target = data.get('target', '')
        key = dm_key(sender, target)
        if key not in direct_messages:
            direct_messages[key] = []
        is_dup = any(
            m.get('timestamp') == ts and m.get('sender') == sender
            for m in direct_messages[key]
        )
        if not is_dup:
            direct_messages[key].append(data)
    elif msg_type == 'channel':
        server_name = data.get('server', DEFAULT_SERVER)
        channel_name = data.get('channel', '')
        srv = servers.get(server_name)
        if srv and channel_name in srv['channels']:
            ch_msgs = srv['channels'][channel_name]['messages']
            is_dup = any(
                m.get('timestamp') == ts and m.get('sender') == sender
                for m in ch_msgs
            )
            if not is_dup:
                ch_msgs.append(data)

    return {"status": "ok"}, 200, {}

# ---------------------------------------------------------------
# Channel management -- creating, listing, and fetching messages
# ---------------------------------------------------------------

@app.route('/servers', methods=['GET'])
def list_servers(headers, body):
    """Returns the servers the user is a member of, with channels."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    srv_list = []
    for name, info in servers.items():
        if user in info['members']:
            ch_list = []
            for ch_name, ch_data in info['channels'].items():
                last_msg = ch_data['messages'][-1] if ch_data['messages'] else None
                ch_list.append({"name": ch_name, "last_message": last_msg})
            srv_list.append({
                "name": name,
                "channels": ch_list,
                "member_count": len(info['members']),
            })

    return {"status": "ok", "servers": srv_list}, 200, {}

@app.route('/servers', methods=['POST'])
def create_server(headers, body):
    """Creates a new server with default channels."""
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
        return {"error": "Server name required"}, 400, {}

    if name in servers:
        return {"error": "Server already exists"}, 400, {}

    servers[name] = {
        "members": {user},
        "channels": {
            "chung": {"messages": []},
            "lạc-đề": {"messages": []},
        },
    }

    print("[ChatApp] Server '{}' created by {}".format(name, user))
    return {"status": "ok", "server": name}, 200, {}

@app.route('/server-channels', methods=['POST'])
def create_server_channel(headers, body):
    """Creates a new channel within a server."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', '')
    ch_name = data.get('name', '').strip()

    if not server_name or not ch_name:
        return {"error": "Server and channel name required"}, 400, {}

    srv = servers.get(server_name)
    if not srv:
        return {"error": "Server not found"}, 404, {}

    if ch_name in srv['channels']:
        return {"error": "Channel already exists in this server"}, 400, {}

    srv['channels'][ch_name] = {"messages": []}

    print("[ChatApp] Channel #{} created in '{}' by {}".format(ch_name, server_name, user))
    return {"status": "ok", "server": server_name, "channel": ch_name}, 200, {}

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
    """Fetches messages for a server channel or DM, filtered by timestamp."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', '')
    channel_name = data.get('channel', '')
    dm_target = data.get('dm', '')
    since = data.get('since', 0)

    msgs = []
    context_name = ""

    if dm_target:
        key = dm_key(user, dm_target)
        all_msgs = direct_messages.get(key, [])
        msgs = [m for m in all_msgs if m.get('timestamp', 0) > since]
        context_name = dm_target
    elif server_name and channel_name:
        srv = servers.get(server_name, {})
        ch = srv.get('channels', {}).get(channel_name, {})
        all_msgs = ch.get('messages', [])
        msgs = [m for m in all_msgs if m.get('timestamp', 0) > since]
        context_name = channel_name

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
        "role": get_user_role(user),
        "display_name": get_display_name(user),
        "peer_info": peers.get(user, {}),
    }, 200, {}

@app.route('/conversations', methods=['GET'])
async def get_conversations(headers, body):
    """Returns DM conversations for the sidebar."""
    user = get_session_user(headers)
    if not user:
        return {"error": "Not authenticated"}, 401, {}

    check_stale_peers()

    convos = []
    now = time.time()

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
            "display_name": get_display_name(other),
            "last_message": last_msg,
            "last_time": last_msg['timestamp'] if last_msg else 0,
            "online": is_online,
            "role": get_user_role(other),
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
            "display_name": get_display_name(uname),
            "last_message": None,
            "last_time": 0,
            "online": is_online,
            "role": get_user_role(uname),
        })

    convos.sort(key=lambda c: c['last_time'], reverse=True)

    return {"status": "ok", "conversations": convos}, 200, {}


# ---------------------------------------------------------------
# Admin-only routes -- privileged actions requiring admin role.
# Regular users get a 403 Forbidden response.
# ---------------------------------------------------------------

@app.route('/admin/users', methods=['GET'])
def admin_list_users(headers, body):
    """Admin-only: list all registered accounts with online status."""
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    accounts = load_accounts()
    now = time.time()
    users = []
    for uname, data in accounts.items():
        peer_info = peers.get(uname, {})
        is_online = peer_info.get('online', False) and \
            (now - peer_info.get('last_seen', 0) < 120)
        users.append({
            "username": uname,
            "display_name": data.get('display_name', uname),
            "role": data.get('role', 'user'),
            "online": is_online,
        })
    return {"status": "ok", "users": users}, 200, {}

@app.route('/admin/kick-user', methods=['POST'])
def admin_kick_user(headers, body):
    """Admin-only: force a user offline and invalidate their sessions."""
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    target = data.get('username', '')
    if not target:
        return {"error": "Username required"}, 400, {}
    if target == user:
        return {"error": "Cannot kick yourself"}, 400, {}

    if target in peers:
        peers[target]['online'] = False
        notify_user_offline(target)

    tokens_to_remove = [t for t, s in sessions.items() if s['username'] == target]
    for t in tokens_to_remove:
        sessions.pop(t, None)

    print("[ChatApp] Admin {} kicked user {}".format(user, target))
    return {"status": "ok", "message": "User {} kicked".format(target)}, 200, {}

@app.route('/admin/delete-channel', methods=['POST'])
def admin_delete_channel(headers, body):
    """Admin-only: delete a non-default channel from a server."""
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', DEFAULT_SERVER)
    channel_name = data.get('channel', '')
    if not channel_name:
        return {"error": "Channel name required"}, 400, {}

    srv = servers.get(server_name)
    if not srv:
        return {"error": "Server not found"}, 404, {}

    if server_name == DEFAULT_SERVER and channel_name in DEFAULT_CHANNELS:
        return {"error": "Cannot delete default channels"}, 400, {}

    if channel_name not in srv['channels']:
        return {"error": "Channel not found"}, 404, {}

    del srv['channels'][channel_name]
    print("[ChatApp] Admin {} deleted #{} from '{}'".format(user, channel_name, server_name))
    return {"status": "ok", "message": "Channel #{} deleted".format(channel_name)}, 200, {}

@app.route('/admin/delete-account', methods=['POST'])
def admin_delete_account(headers, body):
    """Admin-only: permanently delete a user account."""
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    target = data.get('username', '')
    if not target:
        return {"error": "Username required"}, 400, {}
    if target == user:
        return {"error": "Cannot delete your own account"}, 400, {}

    accounts = load_accounts()
    if target not in accounts:
        return {"error": "Account not found"}, 404, {}
    if accounts[target].get('role') == 'admin':
        return {"error": "Cannot delete another admin account"}, 400, {}

    del accounts[target]
    save_accounts(accounts)

    if target in peers:
        peers[target]['online'] = False
    tokens_to_remove = [t for t, s in sessions.items() if s['username'] == target]
    for t in tokens_to_remove:
        sessions.pop(t, None)

    print("[ChatApp] Admin {} deleted account {}".format(user, target))
    return {"status": "ok", "message": "Account {} deleted".format(target)}, 200, {}

@app.route('/admin/all-conversations', methods=['GET'])
def admin_all_conversations(headers, body):
    """Admin-only: read all conversations (channels + DMs) on the server.

    Only accessible from the host server (127.0.0.1). Returns all channel
    messages and all direct message conversations so the admin can monitor
    everything happening on the server.
    """
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    if not is_host_server(headers):
        return {"error": "Only accessible from host server (127.0.0.1)"}, 403, {}

    # Collect all channel messages from all servers
    all_channels = []
    for srv_name, srv in servers.items():
        for ch_name, ch_data in srv['channels'].items():
            all_channels.append({
                "server": srv_name,
                "channel": ch_name,
                "messages": ch_data['messages'][-200:],
                "count": len(ch_data['messages']),
            })

    # Collect all DM conversations
    all_dms = []
    for key, msgs in direct_messages.items():
        parts = key.split(':')
        all_dms.append({
            "users": parts,
            "key": key,
            "messages": msgs[-200:],
            "count": len(msgs),
        })

    # Collect all users with their online status
    accounts = load_accounts()
    now = time.time()
    online_users = []
    for uname, data in accounts.items():
        peer_info = peers.get(uname, {})
        is_online = peer_info.get('online', False) and \
            (now - peer_info.get('last_seen', 0) < 120)
        online_users.append({
            "username": uname,
            "display_name": data.get('display_name', uname),
            "role": data.get('role', 'user'),
            "online": is_online,
            "ip": peer_info.get('ip', ''),
            "port": peer_info.get('port', 0),
            "last_seen": peer_info.get('last_seen', 0),
        })

    return {
        "status": "ok",
        "channels": all_channels,
        "direct_messages": all_dms,
        "users": online_users,
    }, 200, {}

@app.route('/admin/send-to-channel', methods=['POST'])
async def admin_send_to_channel(headers, body):
    """Admin-only: send a message to any channel from the monitor dashboard.

    This allows the admin to participate in conversations while viewing
    the monitor. Only accessible from the host server.
    """
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    if not is_host_server(headers):
        return {"error": "Only accessible from host server"}, 403, {}

    data = body if isinstance(body, dict) else {}
    try:
        if isinstance(body, str):
            data = json.loads(body)
    except Exception:
        data = {}

    server_name = data.get('server', DEFAULT_SERVER)
    channel_name = data.get('channel', '')
    message_text = data.get('message', '')

    if not message_text or not channel_name:
        return {"error": "Missing channel or message"}, 400, {}

    srv = servers.get(server_name)
    if not srv:
        return {"error": "Server not found"}, 404, {}

    ch = srv['channels'].get(channel_name)
    if not ch:
        return {"error": "Channel not found"}, 404, {}

    msg = {
        "sender": user,
        "message": message_text,
        "server": server_name,
        "channel": channel_name,
        "timestamp": time.time(),
        "type": "channel",
    }
    ch['messages'].append(msg)

    # Forward to all other online members
    for member in srv['members']:
        if member != user:
            add_notification(member, "New message in #{} from {}".format(channel_name, user))
            asyncio.create_task(_safe_forward(member, msg))

    return {"status": "ok", "message": msg}, 200, {}

@app.route('/admin/send-to-dm', methods=['POST'])
async def admin_send_to_dm(headers, body):
    """Admin-only: send a DM from the monitor dashboard.

    The admin can send a direct message to any DM conversation visible
    on the monitor. Only accessible from the host server.
    """
    user = get_session_user(headers)
    if not user or get_user_role(user) != 'admin':
        return {"error": "Admin access required"}, 403, {}

    if not is_host_server(headers):
        return {"error": "Only accessible from host server"}, 403, {}

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
    asyncio.create_task(_safe_forward(target, msg))

    return {"status": "ok", "message": msg}, 200, {}


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
