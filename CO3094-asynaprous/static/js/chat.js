/**
 * BK Discordmess -- Discord-style chat client
 *
 * Layout: server strip | channel/DM sidebar | chat area | members panel
 * Polls /messages every 2s, heartbeats every 30s.
 */

let currentUser = null;
let currentUserRole = 'user';
let sessionToken = null;
let serverList = [];
let currentView = 'dm';
let currentServer = null;
let currentChat = null;
let chatHistoryCache = {};
let pollingInterval = null;
let heartbeatInterval = null;
let isLoadingMessages = false;
let knownAdminUsers = new Set();
let membersVisible = false;

const POLL_MS = 2000;
const HEARTBEAT_MS = 30000;
const MSG_GROUP_GAP = 300;
const AVATAR_COLORS = [
    '#5865f2', '#eb459e', '#fee75c', '#57f287',
    '#ed4245', '#3ba55c', '#faa61a', '#5865f2',
    '#e67e22', '#1abc9c', '#e91e63', '#9b59b6',
];

(function() {
    function setHeight() {
        document.documentElement.style.setProperty('--app-height', window.innerHeight + 'px');
    }
    window.addEventListener('resize', setHeight);
    setHeight();
})();

function avatarColor(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
    return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length];
}

function initial(name) { return name.charAt(0).toUpperCase(); }

function chatKey(chat) {
    if (!chat) return '';
    return chat.type === 'channel'
        ? 'ch:' + chat.server + '/' + chat.channel
        : 'dm:' + chat.name;
}

function msgKey(msg) { return msg.sender + '|' + msg.timestamp + '|' + msg.message; }

function formatTime(ts) {
    var d = new Date(ts * 1000);
    var now = new Date();
    var hh = d.getHours().toString().padStart(2, '0');
    var mm = d.getMinutes().toString().padStart(2, '0');
    if (d.toDateString() === now.toDateString()) return 'Today at ' + hh + ':' + mm;
    var yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday at ' + hh + ':' + mm;
    return (d.getMonth() + 1) + '/' + d.getDate() + '/' + d.getFullYear() + ' ' + hh + ':' + mm;
}

function shortTime(ts) {
    var d = new Date(ts * 1000);
    return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
}

function esc(t) {
    if (!t) return '';
    return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(msg) {
    var c = document.getElementById('toastContainer');
    var t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(function() { t.remove(); }, 3000);
}

function getCache(id) {
    if (!chatHistoryCache[id]) chatHistoryCache[id] = { keys: new Set(), ts: 0, messages: [] };
    return chatHistoryCache[id];
}

function hideModal(id) { document.getElementById(id).classList.add('hidden'); }
function showModal(id) { document.getElementById(id).classList.remove('hidden'); }

// ======= Theme =======

function initTheme() {
    var saved = localStorage.getItem('bk-theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    updateThemeIcon(saved);
}

function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('bk-theme', next);
    updateThemeIcon(next);
}

function updateThemeIcon(theme) {
    var icon = document.getElementById('themeIcon');
    if (theme === 'dark') {
        icon.innerHTML = '<path d="M21 12.79A9 9 0 1111.21 3a7 7 0 009.79 9.79z"/>';
    } else {
        icon.innerHTML = '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>';
    }
}

// ======= API =======

async function apiGet(path) {
    try {
        var h = {};
        if (sessionToken) h['X-Session-Token'] = sessionToken;
        var r = await fetch(path, { credentials: 'same-origin', headers: h });
        if (r.status === 401) { window.location.href = '/login'; return null; }
        return await r.json();
    } catch (e) { return null; }
}

async function apiPost(path, body) {
    try {
        var h = { 'Content-Type': 'application/json' };
        if (sessionToken) h['X-Session-Token'] = sessionToken;
        var r = await fetch(path, {
            method: 'POST', headers: h,
            body: JSON.stringify(body),
            credentials: 'same-origin',
        });
        if (r.status === 401) { window.location.href = '/login'; return null; }
        return await r.json();
    } catch (e) { return null; }
}

// ======= Init =======

async function init() {
    initTheme();
    sessionToken = localStorage.getItem('session_token');

    var me = await apiGet('/me');
    if (!me || me.error) { window.location.href = '/login'; return; }
    currentUser = me.username;
    currentUserRole = me.role || 'user';

    if (!sessionToken && me.token) {
        sessionToken = me.token;
        localStorage.setItem('session_token', me.token);
    }

    document.getElementById('userName').textContent = currentUser;
    var av = document.getElementById('userAvatar');
    av.textContent = initial(currentUser);
    av.style.background = avatarColor(currentUser);

    if (currentUserRole === 'admin') {
        document.getElementById('adminBtn').style.display = '';
        knownAdminUsers.add(currentUser);
        document.getElementById('userName').innerHTML =
            esc(currentUser) + ' <span class="admin-badge">ADMIN</span>';
    }

    var serverPort = parseInt(window.location.port) || 80;
    await apiPost('/submit-info', {
        ip: window.location.hostname || '127.0.0.1',
        port: serverPort,
        peer_port: serverPort,
    });

    await loadServers();
    selectView('dm');
    startPolling();
    startHeartbeat();
}

// ======= Servers =======

async function loadServers() {
    var data = await apiGet('/servers');
    if (!data || !data.servers) return;
    serverList = data.servers;
    renderServerStrip();
}

function renderServerStrip() {
    var list = document.getElementById('serverNavList');
    list.innerHTML = '';
    serverList.forEach(function(srv) {
        var icon = document.createElement('div');
        icon.className = 'server-icon' + (currentView === srv.name ? ' active' : '');
        icon.title = srv.name;
        icon.textContent = initial(srv.name);
        icon.style.background = currentView === srv.name ? '' : avatarColor(srv.name);
        icon.style.color = '#fff';
        icon.onclick = function() { selectView(srv.name); };
        list.appendChild(icon);
    });
}

function showCreateServerModal() {
    document.getElementById('newServerName').value = '';
    showModal('createServerModal');
    document.getElementById('newServerName').focus();
}

async function createServer() {
    var name = document.getElementById('newServerName').value.trim();
    if (!name) return;
    var data = await apiPost('/servers', { name: name });
    if (data && data.status === 'ok') {
        hideModal('createServerModal');
        showToast('Server "' + name + '" created!');
        await loadServers();
        selectView(name);
    } else if (data && data.error) {
        showToast(data.error);
    }
}

// ======= View Switching =======

function selectView(view) {
    currentView = view;

    document.getElementById('dmNavBtn').classList.toggle('active', view === 'dm');
    document.querySelectorAll('#serverNavList .server-icon').forEach(function(el) {
        var isActive = el.title === view;
        el.classList.toggle('active', isActive);
        el.style.background = isActive ? '' : avatarColor(el.title);
    });

    if (view === 'dm') {
        currentServer = null;
        renderDMSidebar();
        document.getElementById('membersToggle').style.display = 'none';
    } else {
        currentServer = view;
        renderServerSidebar(view);
        document.getElementById('membersToggle').style.display = '';
    }
}

// ======= DM Sidebar =======

async function renderDMSidebar() {
    var header = document.getElementById('sidebarHeader');
    header.innerHTML = '<input class="sidebar-header-search" id="searchInput" ' +
        'placeholder="Find or start a conversation" oninput="filterSidebar()">';

    var data = await apiGet('/conversations');
    if (!data || !data.conversations) return;

    var content = document.getElementById('sidebarContent');
    content.innerHTML = '';

    var dms = data.conversations.filter(function(c) { return c.type === 'dm'; });

    var section = document.createElement('div');
    section.className = 'dm-section-header';
    section.innerHTML = '<span class="dm-section-title">Direct Messages</span>';
    content.appendChild(section);

    dms.forEach(function(dm) {
        if (dm.role === 'admin') knownAdminUsers.add(dm.name);
        var item = document.createElement('div');
        var isActive = currentChat && currentChat.type === 'dm' && currentChat.name === dm.name;
        item.className = 'dm-item' + (isActive ? ' active' : '');
        item.onclick = function() { openDM(dm.name); };

        var color = avatarColor(dm.name);
        item.innerHTML =
            '<div class="dm-avatar" style="background:' + color + '">' +
                initial(dm.name) +
                '<div class="status-dot ' + (dm.online ? 'online' : '') + '"></div>' +
            '</div>' +
            '<span class="dm-name">' + esc(dm.display_name || dm.name) + '</span>';
        content.appendChild(item);
    });
}

// ======= Server Sidebar =======

function renderServerSidebar(serverName) {
    var srv = serverList.find(function(s) { return s.name === serverName; });
    if (!srv) return;

    var header = document.getElementById('sidebarHeader');
    header.innerHTML = '<span class="server-name-header">' + esc(serverName) + '</span>';

    var content = document.getElementById('sidebarContent');
    content.innerHTML = '';

    var cat = document.createElement('div');
    cat.className = 'channel-category';
    cat.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
            '<polyline points="6 9 12 15 18 9"/>' +
        '</svg>' +
        '<span class="channel-category-name">Text Channels</span>' +
        '<button class="channel-add-btn" onclick="showCreateChannelModal()" title="Create Channel">' +
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">' +
                '<path d="M20 11h-7V4a1 1 0 00-2 0v7H4a1 1 0 000 2h7v7a1 1 0 002 0v-7h7a1 1 0 000-2z"/>' +
            '</svg>' +
        '</button>';
    content.appendChild(cat);

    (srv.channels || []).forEach(function(ch) {
        var chName = ch.name || ch;
        var item = document.createElement('div');
        var isActive = currentChat && currentChat.type === 'channel' &&
            currentChat.server === serverName && currentChat.channel === chName;
        item.className = 'channel-item' + (isActive ? ' active' : '');
        item.onclick = function() { openChannel(serverName, chName); };
        item.innerHTML =
            '<span class="hash">#</span>' +
            '<span class="channel-item-name">' + esc(chName) + '</span>';
        content.appendChild(item);
    });
}

function showCreateChannelModal() {
    document.getElementById('newChannelName').value = '';
    showModal('createChannelModal');
    document.getElementById('newChannelName').focus();
}

async function createChannel() {
    if (!currentServer) return;
    var name = document.getElementById('newChannelName').value.trim().toLowerCase().replace(/\s+/g, '-');
    if (!name) return;
    var data = await apiPost('/server-channels', { server: currentServer, name: name });
    if (data && data.status === 'ok') {
        hideModal('createChannelModal');
        showToast('Channel #' + name + ' created');
        await loadServers();
        renderServerSidebar(currentServer);
        openChannel(currentServer, name);
    } else if (data && data.error) {
        showToast(data.error);
    }
}

// ======= Open Chat =======

function openChannel(serverName, channelName) {
    var newKey = 'ch:' + serverName + '/' + channelName;
    var oldKey = currentChat ? chatKey(currentChat) : '';
    if (newKey === oldKey) return;

    currentChat = { type: 'channel', server: serverName, channel: channelName };

    document.getElementById('chatHashIcon').textContent = '#';
    document.getElementById('chatHashIcon').style.display = '';
    document.getElementById('chatTitle').textContent = channelName;
    document.getElementById('messageInput').placeholder = 'Message #' + channelName;

    var ws = document.getElementById('welcomeScreen');
    if (ws) ws.style.display = 'none';
    document.getElementById('inputArea').style.display = '';
    document.getElementById('messageInput').value = '';

    var delBtn = document.getElementById('deleteChannelBtn');
    delBtn.style.display = (currentUserRole === 'admin') ? '' : 'none';

    renderCached(newKey);
    fetchMessages(true);
    renderServerSidebar(serverName);
    if (membersVisible) loadMembers();
}

function openDM(username) {
    if (currentView !== 'dm') {
        currentView = 'dm';
        currentServer = null;
        document.getElementById('dmNavBtn').classList.add('active');
        document.querySelectorAll('#serverNavList .server-icon').forEach(function(el) {
            el.classList.remove('active');
            el.style.background = avatarColor(el.title);
        });
    }

    var newKey = 'dm:' + username;
    var oldKey = currentChat ? chatKey(currentChat) : '';
    if (newKey === oldKey) return;

    currentChat = { type: 'dm', name: username };

    document.getElementById('chatHashIcon').textContent = '@';
    document.getElementById('chatHashIcon').style.display = '';
    document.getElementById('chatTitle').textContent = username;
    document.getElementById('messageInput').placeholder = 'Message @' + username;

    var ws = document.getElementById('welcomeScreen');
    if (ws) ws.style.display = 'none';
    document.getElementById('inputArea').style.display = '';
    document.getElementById('messageInput').value = '';
    document.getElementById('deleteChannelBtn').style.display = 'none';
    document.getElementById('membersToggle').style.display = 'none';

    renderCached(newKey);
    apiPost('/connect-peer', { target: username });
    fetchMessages(true);
    renderDMSidebar();
}

function renderCached(key) {
    var container = document.getElementById('messagesContainer');
    container.innerHTML = '<div class="messages-inner" id="messagesInner"></div>';
    var inner = document.getElementById('messagesInner');
    var cache = getCache(key);
    renderMessages(inner, cache.messages);
    container.scrollTop = container.scrollHeight;
}

// ======= Messages =======

function renderMessages(container, messages) {
    container.innerHTML = '';

    if (currentChat && currentChat.type === 'channel') {
        var welcome = document.createElement('div');
        welcome.className = 'channel-welcome';
        welcome.innerHTML =
            '<div class="channel-welcome-icon">#</div>' +
            '<h2>Welcome to #' + esc(currentChat.channel) + '</h2>' +
            '<p>This is the start of the #' + esc(currentChat.channel) + ' channel.</p>';
        container.appendChild(welcome);
    }

    for (var i = 0; i < messages.length; i++) {
        var msg = messages[i];
        var prev = i > 0 ? messages[i - 1] : null;
        var isGroupStart = !prev ||
            prev.sender !== msg.sender ||
            (msg.timestamp - prev.timestamp) > MSG_GROUP_GAP;

        var div = document.createElement('div');
        div.className = 'msg-group' + (isGroupStart ? ' msg-group-start' : '');

        var html = '';
        if (isGroupStart) {
            var color = avatarColor(msg.sender);
            html += '<div class="msg-avatar" style="background:' + color + '">' +
                initial(msg.sender) + '</div>';
            html += '<div class="msg-header">';
            html += '<span class="msg-author">' + esc(msg.sender);
            if (knownAdminUsers.has(msg.sender)) html += ' <span class="admin-badge">ADMIN</span>';
            html += '</span>';
            html += '<span class="msg-timestamp">' + formatTime(msg.timestamp) + '</span>';
            html += '</div>';
        } else {
            html += '<span class="msg-hover-time">' + shortTime(msg.timestamp) + '</span>';
        }
        html += '<div class="msg-content">' + esc(msg.message) + '</div>';

        div.innerHTML = html;
        container.appendChild(div);
    }
}

async function fetchMessages(force) {
    if (!currentChat) return;
    if (isLoadingMessages && !force) return;
    isLoadingMessages = true;

    try {
        var key = chatKey(currentChat);
        var cache = getCache(key);

        var body = { since: cache.ts };
        if (currentChat.type === 'channel') {
            body.server = currentChat.server;
            body.channel = currentChat.channel;
        } else {
            body.dm = currentChat.name;
        }

        var data = await apiPost('/messages', body);
        if (!data || !data.messages) return;

        var activeKey = currentChat ? chatKey(currentChat) : '';
        if (key !== activeKey) return;

        var container = document.getElementById('messagesContainer');
        var wasAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 60;
        var added = false;

        data.messages.forEach(function(msg) {
            var k = msgKey(msg);
            if (!cache.keys.has(k)) {
                var isDup = cache.messages.some(function(ex) {
                    return ex.sender === msg.sender &&
                        ex.message === msg.message &&
                        Math.abs(ex.timestamp - msg.timestamp) < 10;
                });
                if (!isDup) {
                    cache.keys.add(k);
                    cache.messages.push(msg);
                    added = true;
                } else {
                    cache.keys.add(k);
                }
            }
            if (msg.timestamp > cache.ts) cache.ts = msg.timestamp;
        });

        if (added) {
            var inner = document.getElementById('messagesInner');
            if (inner) renderMessages(inner, cache.messages);
            if (wasAtBottom) container.scrollTop = container.scrollHeight;
        }
    } finally {
        isLoadingMessages = false;
    }
}

// ======= Send Message =======

async function sendMessage() {
    var input = document.getElementById('messageInput');
    var text = input.value.trim();
    if (!text || !currentChat) return;
    input.value = '';
    input.focus();

    var key = chatKey(currentChat);
    var cache = getCache(key);
    var now = Date.now() / 1000;

    var optimistic = { sender: currentUser, message: text, timestamp: now };
    cache.keys.add(msgKey(optimistic));
    cache.messages.push(optimistic);

    var inner = document.getElementById('messagesInner');
    if (inner) renderMessages(inner, cache.messages);
    var container = document.getElementById('messagesContainer');
    container.scrollTop = container.scrollHeight;

    var result;
    if (currentChat.type === 'channel') {
        result = await apiPost('/broadcast-peer', {
            server: currentChat.server,
            channel: currentChat.channel,
            message: text,
        });
    } else {
        result = await apiPost('/send-peer', { target: currentChat.name, message: text });
    }

    if (result && result.message) {
        cache.keys.add(msgKey(result.message));
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

        if (currentView === 'dm') {
            await renderDMSidebar();
        }
    }, POLL_MS);
}

function startHeartbeat() {
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(function() { apiPost('/heartbeat', {}); }, HEARTBEAT_MS);
}

// ======= Members Panel =======

function toggleMembers() {
    membersVisible = !membersVisible;
    var panel = document.getElementById('membersPanel');
    panel.classList.toggle('hidden', !membersVisible);
    if (membersVisible) loadMembers();
}

async function loadMembers() {
    var panel = document.getElementById('membersPanel');
    var data = await apiGet('/get-list');
    if (!data || !data.peers) return;

    var online = data.peers.filter(function(p) { return p.online; });
    var offline = data.peers.filter(function(p) { return !p.online; });

    var html = '';
    if (online.length > 0) {
        html += '<div class="members-category">Online \u2014 ' + online.length + '</div>';
        online.forEach(function(p) {
            html += memberHTML(p, false);
        });
    }
    if (offline.length > 0) {
        html += '<div class="members-category">Offline \u2014 ' + offline.length + '</div>';
        offline.forEach(function(p) {
            html += memberHTML(p, true);
        });
    }
    panel.innerHTML = html;
}

function memberHTML(peer, isOffline) {
    var color = avatarColor(peer.username);
    return '<div class="member-item' + (isOffline ? ' offline' : '') + '" onclick="openDM(\'' + esc(peer.username) + '\')">' +
        '<div class="member-item-avatar" style="background:' + color + '">' +
            initial(peer.username) + '</div>' +
        '<span class="member-item-name">' + esc(peer.username) +
            (peer.role === 'admin' ? ' <span class="admin-badge">ADMIN</span>' : '') +
        '</span></div>';
}

// ======= Search =======

function filterSidebar() {
    var q = (document.getElementById('searchInput') || {}).value;
    if (!q) q = '';
    q = q.toLowerCase();
    document.querySelectorAll('.dm-item, .channel-item').forEach(function(el) {
        var name = el.textContent.toLowerCase();
        el.style.display = name.includes(q) ? '' : 'none';
    });
}

// ======= Logout =======

async function handleLogout() {
    currentUser = null;
    await apiPost('/logout', {});
    localStorage.removeItem('session_token');
    if (pollingInterval) clearInterval(pollingInterval);
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    window.location.href = '/login';
}

// ======= Admin =======

async function showAdminPanel() {
    showModal('adminModal');
    var data = await apiGet('/admin/users');
    if (!data || !data.users) return;

    var html = '';
    data.users.forEach(function(u) {
        var color = avatarColor(u.username);
        html += '<div class="admin-user-item">' +
            '<div class="member-item-avatar" style="background:' + color + '">' +
                initial(u.username) + '</div>' +
            '<div class="admin-user-info">' +
                '<div class="admin-user-name">' + esc(u.display_name) +
                    (u.role === 'admin' ? ' <span class="admin-badge">ADMIN</span>' : '') +
                '</div>' +
                '<div class="admin-user-meta">' +
                    (u.online ? 'Online' : 'Offline') + ' \u00B7 @' + esc(u.username) +
                '</div>' +
            '</div>' +
            '<div class="admin-actions">';
        if (u.role !== 'admin') {
            if (u.online) {
                html += '<button class="btn-kick" onclick="kickUser(\'' + u.username + '\')">Kick</button>';
            }
            html += '<button class="btn-delete-acct" onclick="deleteAccount(\'' + u.username + '\')">Delete</button>';
        }
        html += '</div></div>';
    });
    document.getElementById('adminUserList').innerHTML = html;
}

async function kickUser(username) {
    if (!confirm('Kick ' + username + '?')) return;
    var data = await apiPost('/admin/kick-user', { username: username });
    if (data && data.status === 'ok') {
        showToast('Kicked ' + username);
        showAdminPanel();
    } else if (data && data.error) {
        showToast(data.error);
    }
}

async function deleteAccount(username) {
    if (!confirm('Permanently delete "' + username + '"?')) return;
    var data = await apiPost('/admin/delete-account', { username: username });
    if (data && data.status === 'ok') {
        showToast('Account ' + username + ' deleted');
        showAdminPanel();
    } else if (data && data.error) {
        showToast(data.error);
    }
}

async function deleteCurrentChannel() {
    if (!currentChat || currentChat.type !== 'channel') return;
    if (!confirm('Delete channel #' + currentChat.channel + '?')) return;
    var data = await apiPost('/admin/delete-channel', {
        server: currentChat.server,
        channel: currentChat.channel,
    });
    if (data && data.status === 'ok') {
        showToast('Channel #' + currentChat.channel + ' deleted');
        currentChat = null;
        document.getElementById('chatTitle').textContent = 'BK Discordmess';
        document.getElementById('welcomeScreen').style.display = '';
        document.getElementById('inputArea').style.display = 'none';
        document.getElementById('deleteChannelBtn').style.display = 'none';
        await loadServers();
        if (currentServer) renderServerSidebar(currentServer);
    } else if (data && data.error) {
        showToast(data.error);
    }
}

// ======= Start =======
document.addEventListener('DOMContentLoaded', init);
