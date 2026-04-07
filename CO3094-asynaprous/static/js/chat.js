/**
 * BK-Messenger -- client-side chat logic
 *
 * This runs in the browser and talks to our Python server via fetch().
 * All the async polling, message display, and conversation switching
 * happens here. The server just stores data and returns JSON.
 *
 * Key design decisions:
 * - We poll /messages every 2s instead of WebSockets (simpler, HTTP-only)
 * - Each conversation has its own cache so switching chats is instant
 * - Messages are shown optimistically (appear before server confirms)
 */

let currentUser = null;
let currentChat = null;       // which conversation is open right now
let chatHistoryCache = {};    // per-conversation cache: { keys: Set, ts: number, messages: [] }
let pollingInterval = null;
let heartbeatInterval = null;
let isLoadingMessages = false; // prevents double-loading from poll + manual load
let sessionToken = null;      // per-tab auth token stored in sessionStorage

const POLL_MS = 2000;          // check for new messages every 2 seconds
const HEARTBEAT_MS = 30000;    // tell server we're still alive every 30s

const AVATAR_COLORS = [
    '#0032A0', '#0078D4', '#e17055', '#00b894',
    '#6c5ce7', '#fdcb6e', '#d63031', '#00cec9',
];

(function() {
    function setAppHeight() {
        document.documentElement.style.setProperty('--app-height', window.innerHeight + 'px');
    }
    window.addEventListener('resize', setAppHeight);
    window.addEventListener('orientationchange', function() { setTimeout(setAppHeight, 150); });
    setAppHeight();
})();

function getAvatarColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
    return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length];
}

function getInitials(name) { return name.charAt(0).toUpperCase(); }

function chatId(type, name) { return type === 'channel' ? 'ch:' + name : 'dm:' + name; }

function msgKey(msg) { return msg.sender + '|' + msg.timestamp + '|' + msg.message; }

function formatTime(ts) {
    var d = new Date(ts * 1000), now = new Date();
    var hh = d.getHours().toString().padStart(2, '0');
    var mm = d.getMinutes().toString().padStart(2, '0');
    if (d.toDateString() === now.toDateString()) return hh + ':' + mm;
    return d.getMonth() + 1 + '/' + d.getDate() + ' ' + hh + ':' + mm;
}

function escapeHtml(t) {
    if (!t) return '';
    return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showToast(msg) {
    var c = document.getElementById('toastContainer');
    var t = document.createElement('div');
    t.className = 'toast'; t.textContent = msg;
    c.appendChild(t);
    setTimeout(function() { t.remove(); }, 3000);
}

function getCache(id) {
    if (!chatHistoryCache[id]) chatHistoryCache[id] = { keys: new Set(), ts: 0, messages: [] };
    return chatHistoryCache[id];
}

// ======= API =======

async function apiGet(path) {
    try {
        var h = {};
        if (sessionToken) h['X-Session-Token'] = sessionToken;
        var r = await fetch(path, { credentials: 'same-origin', headers: h });
        if (r.status === 401) { window.location.href = '/login'; return null; }
        return await r.json();
    } catch(e) { return null; }
}

async function apiPost(path, body) {
    try {
        var h = { 'Content-Type': 'application/json' };
        if (sessionToken) h['X-Session-Token'] = sessionToken;
        var r = await fetch(path, {
            method: 'POST',
            headers: h,
            body: JSON.stringify(body),
            credentials: 'same-origin',
        });
        if (r.status === 401) { window.location.href = '/login'; return null; }
        return await r.json();
    } catch(e) { return null; }
}

// ======= Init =======

async function init() {
    sessionToken = sessionStorage.getItem('session_token');

    var me = await apiGet('/me');
    if (!me || me.error) { window.location.href = '/login'; return; }
    currentUser = me.username;

    if (!sessionToken && me.token) {
        sessionToken = me.token;
        sessionStorage.setItem('session_token', me.token);
    }

    document.getElementById('currentUserName').textContent = currentUser;
    document.getElementById('currentUserAvatar').textContent = getInitials(currentUser);
    document.getElementById('currentUserAvatar').style.background = getAvatarColor(currentUser);

    var serverPort = parseInt(window.location.port) || 80;
    await apiPost('/submit-info', {
        ip: window.location.hostname || '127.0.0.1',
        port: serverPort,
        peer_port: serverPort,
    });
    await loadConversations();

    startPolling();
    startHeartbeat();
}

// ======= Unified Conversation List (like Messenger sidebar) =======

async function loadConversations() {
    var data = await apiGet('/conversations');
    if (!data || !data.conversations) return;

    var list = document.getElementById('conversationList');
    list.innerHTML = '';

    data.conversations.forEach(function(c) {
        var item = document.createElement('div');
        var isActive = currentChat &&
            ((c.type === 'channel' && currentChat.type === 'channel' && currentChat.name === c.name) ||
             (c.type === 'dm' && currentChat.type === 'dm' && currentChat.name === c.name));
        item.className = 'chat-item' + (isActive ? ' active' : '');

        if (c.type === 'channel') {
            item.onclick = function() { openChat('channel', c.name); };
        } else {
            item.onclick = function() { openChat('dm', c.name); };
        }

        var lm = c.last_message;
        var preview = lm ? (lm.sender + ': ' + lm.message).substring(0, 35) : 'Start a conversation';
        var timeStr = lm ? formatTime(lm.timestamp) : '';
        var avatarClass = c.type === 'channel' ? 'channel' : ('peer' + (c.online ? ' online' : ''));
        var avatarBg = c.type === 'channel' ? '' : ' style="background:' + getAvatarColor(c.name) + '"';

        item.innerHTML =
            '<div class="chat-avatar ' + avatarClass + '"' + avatarBg + '>' +
                getInitials(c.name) +
            '</div>' +
            '<div class="chat-info">' +
                '<div class="chat-name">' + escapeHtml(c.display_name) + '</div>' +
                '<div class="chat-preview">' + escapeHtml(preview) + '</div>' +
            '</div>' +
            '<div class="chat-meta">' +
                '<div class="chat-time">' + timeStr + '</div>' +
            '</div>';
        list.appendChild(item);
    });
}

// ======= Open a Chat =======

function openChat(type, name) {
    var newId = chatId(type, name);
    var oldId = currentChat ? chatId(currentChat.type, currentChat.name) : null;

    if (newId === oldId) return;

    currentChat = { type: type, name: name };

    document.getElementById('chatTitle').textContent = type === 'channel' ? '#' + name : name;
    document.getElementById('chatStatus').textContent =
        type === 'channel' ? 'Channel' : 'Direct Message';
    var ws = document.getElementById('welcomeScreen');
    if (ws) ws.style.display = 'none';
    document.getElementById('inputArea').style.display = 'block';
    document.getElementById('messageInput').value = '';

    renderCachedMessages(newId);

    if (type === 'dm') {
        apiPost('/connect-peer', { target: name });
    }

    loadConversations();
    fetchMessages(true);
}

function renderCachedMessages(id) {
    var container = document.getElementById('messagesContainer');
    container.innerHTML = '';
    var cache = getCache(id);
    cache.messages.forEach(function(msg) {
        appendMessageBubble(msg);
    });
    container.scrollTop = container.scrollHeight;
}

// ======= Messages =======

async function fetchMessages(force) {
    if (!currentChat) return;
    if (isLoadingMessages && !force) return;
    isLoadingMessages = true;

    try {
        var id = chatId(currentChat.type, currentChat.name);
        var cache = getCache(id);

        var body = { since: cache.ts };
        if (currentChat.type === 'channel') {
            body.channel = currentChat.name;
        } else {
            body.dm = currentChat.name;
        }

        var data = await apiPost('/messages', body);
        if (!data || !data.messages) return;

        var activeId = currentChat ? chatId(currentChat.type, currentChat.name) : null;
        if (id !== activeId) return;

        var container = document.getElementById('messagesContainer');
        var wasAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 60;
        var added = false;

        data.messages.forEach(function(msg) {
            var k = msgKey(msg);
            if (!cache.keys.has(k)) {
                var isDuplicate = cache.messages.some(function(existing) {
                    return existing.sender === msg.sender &&
                           existing.message === msg.message &&
                           Math.abs(existing.timestamp - msg.timestamp) < 10;
                });
                if (!isDuplicate) {
                    cache.keys.add(k);
                    cache.messages.push(msg);
                    appendMessageBubble(msg);
                    added = true;
                } else {
                    cache.keys.add(k);
                }
            }
            if (msg.timestamp > cache.ts) cache.ts = msg.timestamp;
        });

        if (added && wasAtBottom) {
            container.scrollTop = container.scrollHeight;
        }
    } finally {
        isLoadingMessages = false;
    }
}

function appendMessageBubble(msg) {
    var container = document.getElementById('messagesContainer');
    var isOwn = msg.sender === currentUser;

    var row = document.createElement('div');
    row.className = 'message-row ' + (isOwn ? 'own' : 'other');

    var color = getAvatarColor(msg.sender);
    var html = '';

    if (!isOwn) {
        html += '<div class="msg-avatar" style="background:' + color + '">' +
                getInitials(msg.sender) + '</div>';
    }

    html += '<div class="msg-body">';
    if (!isOwn && currentChat && currentChat.type === 'channel') {
        html += '<div class="msg-sender">' + escapeHtml(msg.sender) + '</div>';
    }
    html += '<div class="message-bubble">' + escapeHtml(msg.message) + '</div>';
    html += '<div class="msg-time">' + formatTime(msg.timestamp) + '</div>';
    html += '</div>';

    row.innerHTML = html;
    container.appendChild(row);
}

// ======= Send Message =======

async function sendMessage() {
    var input = document.getElementById('messageInput');
    var text = input.value.trim();
    if (!text || !currentChat) return;
    input.value = '';
    input.focus();

    var id = chatId(currentChat.type, currentChat.name);
    var cache = getCache(id);
    var now = Date.now() / 1000;

    var optimisticMsg = { sender: currentUser, message: text, timestamp: now };
    var k = msgKey(optimisticMsg);
    cache.keys.add(k);
    cache.messages.push(optimisticMsg);
    appendMessageBubble(optimisticMsg);

    var container = document.getElementById('messagesContainer');
    container.scrollTop = container.scrollHeight;

    var result;
    if (currentChat.type === 'channel') {
        result = await apiPost('/broadcast-peer', { channel: currentChat.name, message: text });
    } else {
        result = await apiPost('/send-peer', { target: currentChat.name, message: text });
    }

    if (result && result.message) {
        var serverKey = msgKey(result.message);
        cache.keys.add(serverKey);
        if (result.message.timestamp > cache.ts) {
            cache.ts = result.message.timestamp;
        }
    }
}

// ======= Polling =======

function startPolling() {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(async function() {
        await fetchMessages(false);

        var notifs = await apiGet('/notifications');
        if (notifs && notifs.notifications && notifs.notifications.length > 0) {
            notifs.notifications.forEach(function(n) { showToast(n.message); });
        }

        await loadConversations();
    }, POLL_MS);
}

function startHeartbeat() {
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(function() { apiPost('/heartbeat', {}); }, HEARTBEAT_MS);
}

// ======= Channel Management =======

function showNewChannelModal() {
    document.getElementById('newChannelModal').classList.remove('hidden');
    document.getElementById('newChannelName').value = '';
    document.getElementById('newChannelName').focus();
}
function hideNewChannelModal() {
    document.getElementById('newChannelModal').classList.add('hidden');
}
async function createChannel() {
    var name = document.getElementById('newChannelName').value.trim();
    if (!name) return;
    var data = await apiPost('/channels', { name: name });
    if (data && data.status === 'ok') {
        hideNewChannelModal();
        showToast('Channel #' + name + ' created');
        await loadConversations();
        openChat('channel', name);
    } else if (data && data.error) {
        showToast(data.error);
    }
}

// ======= Search =======

function filterSidebar() {
    var q = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.chat-item').forEach(function(item) {
        var name = item.querySelector('.chat-name').textContent.toLowerCase();
        item.style.display = name.includes(q) ? '' : 'none';
    });
}

// ======= Info Panel =======

function showPeerInfo() {
    var p = document.getElementById('infoPanel');
    p.classList.toggle('hidden');
    if (!p.classList.contains('hidden') && currentChat) updateInfoPanel();
}
function toggleInfoPanel() {
    document.getElementById('infoPanel').classList.add('hidden');
}
async function updateInfoPanel() {
    var content = document.getElementById('infoContent');
    var title = document.getElementById('infoPanelTitle');
    if (!currentChat) return;

    if (currentChat.type === 'channel') {
        title.textContent = '#' + currentChat.name;
        var peersData = await apiGet('/get-list');
        var html = '<div class="info-section"><h4>Members</h4>';
        if (peersData && peersData.peers) {
            peersData.peers.forEach(function(p) {
                html += '<div class="member-item">' +
                    '<div class="member-avatar" style="background:' + getAvatarColor(p.username) + '">' +
                        getInitials(p.username) + '</div>' +
                    '<div><div class="member-name">' + escapeHtml(p.username) + '</div>' +
                    '<div class="member-status' + (p.online ? ' status-online' : '') + '">' +
                        (p.online ? 'Online' : 'Offline') + '</div></div></div>';
            });
        }
        content.innerHTML = html + '</div>';
    } else {
        title.textContent = currentChat.name;
        content.innerHTML =
            '<div class="info-section"><div class="member-item">' +
            '<div class="member-avatar" style="background:' + getAvatarColor(currentChat.name) + '">' +
                getInitials(currentChat.name) + '</div>' +
            '<div><div class="member-name">' + escapeHtml(currentChat.name) + '</div>' +
            '<div class="member-status">Direct Message</div></div></div></div>';
    }
}

// ======= Logout =======

async function handleLogout() {
    currentUser = null;
    await apiPost('/logout', {});
    sessionStorage.removeItem('session_token');
    if (pollingInterval) clearInterval(pollingInterval);
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    window.location.href = '/login';
}

window.addEventListener('beforeunload', function() {
    if (currentUser) {
        navigator.sendBeacon('/logout');
    }
});

// ======= Start =======
document.addEventListener('DOMContentLoaded', init);
