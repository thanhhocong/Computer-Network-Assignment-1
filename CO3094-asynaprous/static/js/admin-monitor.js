/**
 * BK Discordmess — Admin Monitor Dashboard
 *
 * This page is only accessible to admins from 127.0.0.1 (localhost).
 * It shows ALL conversations on the server: every channel message and
 * every DM between every user. The admin can also send messages.
 *
 * Polls /admin/all-conversations every 2s to stay updated.
 */

let currentUser = null;
let sessionToken = null;
let currentConv = null;      // { type:'channel', server, channel } or { type:'dm', key, users }
let allChannels = [];
let allDMs = [];
let allUsers = [];
let localCache = {};          // key -> { messages:[], lastCount:0 }
let pollingInterval = null;
let heartbeatInterval = null;

const POLL_MS = 2000;
const HEARTBEAT_MS = 30000;
const MSG_GROUP_GAP = 300;
const COLORS = [
    '#5865f2', '#eb459e', '#fee75c', '#57f287',
    '#ed4245', '#3ba55c', '#faa61a', '#e67e22',
    '#1abc9c', '#e91e63', '#9b59b6',
];

// ======= Helpers =======

function avatarColor(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
    return COLORS[Math.abs(h) % COLORS.length];
}

function initial(name) { return name ? name.charAt(0).toUpperCase() : '?'; }

function esc(t) {
    if (!t) return '';
    return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

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

function timeAgo(ts) {
    if (!ts) return 'Never';
    var diff = Date.now() / 1000 - ts;
    if (diff < 60) return 'Just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}

function convKey(conv) {
    if (!conv) return '';
    return conv.type === 'channel'
        ? 'ch:' + conv.server + '/' + conv.channel
        : 'dm:' + conv.key;
}

function showToast(msg) {
    var c = document.getElementById('toastContainer');
    var t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(function() { t.remove(); }, 3000);
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
    sessionToken = localStorage.getItem('session_token');

    var me = await apiGet('/me');
    if (!me || me.error) { window.location.href = '/login'; return; }
    if (me.role !== 'admin') { window.location.href = '/chat.html'; return; }

    currentUser = me.username;

    if (!sessionToken && me.token) {
        sessionToken = me.token;
        localStorage.setItem('session_token', me.token);
    }

    // Set user info in sidebar
    document.getElementById('monitorUserName').innerHTML =
        esc(currentUser) + ' <span class="admin-tag">ADMIN</span>';
    var av = document.getElementById('monitorUserAvatar');
    av.textContent = initial(currentUser);
    av.style.background = avatarColor(currentUser);

    // Register online
    var serverPort = parseInt(window.location.port) || 80;
    await apiPost('/submit-info', {
        ip: window.location.hostname || '127.0.0.1',
        port: serverPort,
        peer_port: serverPort,
    });

    await fetchAllConversations();
    renderSidebar();
    startPolling();
    startHeartbeat();
}

// ======= Data Fetching =======

async function fetchAllConversations() {
    var data = await apiGet('/admin/all-conversations');
    if (!data || data.error) return;

    allChannels = data.channels || [];
    allDMs = data.direct_messages || [];
    allUsers = data.users || [];

    // Update stats bar
    updateStats();

    // Update online users panel
    renderOnlineUsers();

    // If we're viewing a conversation, check for new messages
    if (currentConv) {
        var key = convKey(currentConv);
        var cached = localCache[key];
        var newMsgs = getMessagesForCurrent();
        if (!cached || newMsgs.length !== cached.lastCount) {
            if (!cached) localCache[key] = {};
            localCache[key].messages = newMsgs;
            localCache[key].lastCount = newMsgs.length;
            renderCurrentMessages();
        }
    }

    // Re-render sidebar to update counts
    renderSidebar();
}

function getMessagesForCurrent() {
    if (!currentConv) return [];
    if (currentConv.type === 'channel') {
        var ch = allChannels.find(function(c) {
            return c.server === currentConv.server && c.channel === currentConv.channel;
        });
        return ch ? ch.messages : [];
    } else {
        var dm = allDMs.find(function(d) { return d.key === currentConv.key; });
        return dm ? dm.messages : [];
    }
}

function updateStats() {
    var totalCh = 0;
    allChannels.forEach(function(c) { totalCh += c.count; });
    var totalDm = 0;
    allDMs.forEach(function(d) { totalDm += d.count; });

    var onlineCount = allUsers.filter(function(u) { return u.online; }).length;

    document.getElementById('statOnline').textContent = onlineCount + '/' + allUsers.length;
    document.getElementById('statChannels').textContent = allChannels.length;
    document.getElementById('statChMsgs').textContent = totalCh;
    document.getElementById('statDMs').textContent = allDMs.length;
    document.getElementById('statDmMsgs').textContent = totalDm;
}

// ======= Online Users Panel =======

function renderOnlineUsers() {
    var container = document.getElementById('onlineUsersList');
    if (!container) return;

    var online = allUsers.filter(function(u) { return u.online; });
    var offline = allUsers.filter(function(u) { return !u.online; });

    var html = '';

    // Online section
    html += '<div class="users-section-label">Online \u2014 ' + online.length + '</div>';
    if (online.length === 0) {
        html += '<div class="users-empty">No users online</div>';
    }
    online.forEach(function(u) {
        var color = avatarColor(u.username);
        html += '<div class="user-item online">' +
            '<div class="user-item-avatar" style="background:' + color + '">' +
                initial(u.username) +
                '<div class="user-status-dot online"></div>' +
            '</div>' +
            '<div class="user-item-info">' +
                '<div class="user-item-name">' + esc(u.display_name || u.username) +
                    (u.role === 'admin' ? ' <span class="admin-tag">ADMIN</span>' : '') +
                '</div>' +
                '<div class="user-item-meta">' +
                    '@' + esc(u.username) +
                    (u.ip ? ' \u00B7 ' + esc(u.ip) + ':' + u.port : '') +
                '</div>' +
            '</div>' +
            '<div class="user-item-actions">' +
                '<button class="user-action-btn" onclick="openDMWithUser(\'' + esc(u.username) + '\')" title="Message">' +
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">' +
                        '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>' +
                    '</svg>' +
                '</button>' +
            '</div>' +
        '</div>';
    });

    // Offline section
    html += '<div class="users-section-label" style="margin-top:12px">Offline \u2014 ' + offline.length + '</div>';
    offline.forEach(function(u) {
        var color = avatarColor(u.username);
        html += '<div class="user-item offline">' +
            '<div class="user-item-avatar" style="background:' + color + ';opacity:0.5">' +
                initial(u.username) +
                '<div class="user-status-dot offline"></div>' +
            '</div>' +
            '<div class="user-item-info">' +
                '<div class="user-item-name" style="opacity:0.5">' + esc(u.display_name || u.username) +
                    (u.role === 'admin' ? ' <span class="admin-tag">ADMIN</span>' : '') +
                '</div>' +
                '<div class="user-item-meta">' +
                    '@' + esc(u.username) +
                    (u.last_seen ? ' \u00B7 Last seen: ' + timeAgo(u.last_seen) : '') +
                '</div>' +
            '</div>' +
        '</div>';
    });

    container.innerHTML = html;
}

function openDMWithUser(username) {
    // Find an existing DM with this user, or create a key
    var existingDM = allDMs.find(function(d) {
        return d.users.indexOf(username) !== -1 && d.users.indexOf(currentUser) !== -1;
    });

    if (existingDM) {
        openDM(existingDM.key, existingDM.users);
    } else {
        // Create a key for a new DM
        var users = [currentUser, username].sort();
        var key = users.join(':');
        openDM(key, users);
    }
}

// ======= Sidebar Rendering =======

function renderSidebar() {
    var container = document.getElementById('sidebarSections');
    container.innerHTML = '';

    // Channel section
    var chLabel = document.createElement('div');
    chLabel.className = 'section-label';
    chLabel.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>' +
        '<span>Text Channels (' + allChannels.length + ')</span>';
    container.appendChild(chLabel);

    // Group channels by server
    var serverMap = {};
    allChannels.forEach(function(c) {
        if (!serverMap[c.server]) serverMap[c.server] = [];
        serverMap[c.server].push(c);
    });

    Object.keys(serverMap).forEach(function(srvName) {
        var srvLabel = document.createElement('div');
        srvLabel.className = 'section-label';
        srvLabel.style.fontSize = '10px';
        srvLabel.style.paddingTop = '8px';
        srvLabel.style.color = 'var(--text-muted)';
        srvLabel.textContent = srvName;
        container.appendChild(srvLabel);

        serverMap[srvName].forEach(function(ch) {
            var item = document.createElement('div');
            var key = 'ch:' + ch.server + '/' + ch.channel;
            var isActive = currentConv && convKey(currentConv) === key;
            item.className = 'conv-item' + (isActive ? ' active' : '');
            item.onclick = function() { openChannel(ch.server, ch.channel); };

            var badge = ch.count > 0 ? '<span class="conv-badge">' + ch.count + '</span>' : '';
            item.innerHTML =
                '<span class="conv-icon">#</span>' +
                '<span class="conv-name">' + esc(ch.channel) + '</span>' +
                badge;
            container.appendChild(item);
        });
    });

    // DM section
    var dmLabel = document.createElement('div');
    dmLabel.className = 'section-label';
    dmLabel.style.marginTop = '8px';
    dmLabel.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>' +
        '<span>Direct Messages (' + allDMs.length + ')</span>';
    container.appendChild(dmLabel);

    if (allDMs.length === 0) {
        var emptyDm = document.createElement('div');
        emptyDm.style.cssText = 'padding:8px 14px;color:var(--text-muted);font-size:12px;font-style:italic';
        emptyDm.textContent = 'No DM conversations yet';
        container.appendChild(emptyDm);
    }

    allDMs.forEach(function(dm) {
        var item = document.createElement('div');
        var key = 'dm:' + dm.key;
        var isActive = currentConv && convKey(currentConv) === key;
        item.className = 'conv-item' + (isActive ? ' active' : '');
        item.onclick = function() { openDM(dm.key, dm.users); };

        var names = dm.users.join(' \u2194 ');
        var color1 = avatarColor(dm.users[0]);
        var color2 = dm.users.length > 1 ? avatarColor(dm.users[1]) : color1;
        var badge = dm.count > 0 ? '<span class="conv-badge">' + dm.count + '</span>' : '';

        // Show two overlapping avatars for DMs
        item.innerHTML =
            '<div class="dm-duo">' +
                '<div class="dm-avatar-small" style="background:' + color1 + '">' + initial(dm.users[0]) + '</div>' +
                (dm.users.length > 1 ? '<div class="dm-avatar-small dm-avatar-overlap" style="background:' + color2 + '">' + initial(dm.users[1]) + '</div>' : '') +
            '</div>' +
            '<span class="conv-name">' + esc(names) + '</span>' +
            badge;
        container.appendChild(item);
    });
}

// ======= Open Conversations =======

function openChannel(server, channel) {
    currentConv = { type: 'channel', server: server, channel: channel };
    var key = convKey(currentConv);
    var msgs = getMessagesForCurrent();
    localCache[key] = { messages: msgs, lastCount: msgs.length };

    // Update header
    document.getElementById('chatHeaderIcon').textContent = '#';
    document.getElementById('chatHeaderTitle').textContent = channel;
    document.getElementById('chatHeaderSub').textContent = server;

    // Update input placeholder
    document.getElementById('monitorInput').placeholder = 'Message #' + channel + ' as Admin';

    // Show input area
    document.getElementById('monitorInputArea').classList.remove('hidden');

    // Hide welcome
    document.getElementById('monitorWelcome').style.display = 'none';
    document.getElementById('monitorMsgsInner').style.display = '';

    renderCurrentMessages();
    renderSidebar();
}

function openDM(dmKey, users) {
    currentConv = { type: 'dm', key: dmKey, users: users };
    var key = convKey(currentConv);
    var msgs = getMessagesForCurrent();
    localCache[key] = { messages: msgs, lastCount: msgs.length };

    var names = users.join(' \u2194 ');
    document.getElementById('chatHeaderIcon').textContent = '@';
    document.getElementById('chatHeaderTitle').textContent = names;
    document.getElementById('chatHeaderSub').textContent = 'Direct Messages';

    var otherUser = users.find(function(u) { return u !== currentUser; }) || users[0];
    document.getElementById('monitorInput').placeholder = 'Message @' + otherUser + ' as Admin';
    document.getElementById('monitorInputArea').classList.remove('hidden');

    document.getElementById('monitorWelcome').style.display = 'none';
    document.getElementById('monitorMsgsInner').style.display = '';

    renderCurrentMessages();
    renderSidebar();
}

// ======= Message Rendering =======

function renderCurrentMessages() {
    var container = document.getElementById('monitorMsgsInner');
    container.innerHTML = '';

    var msgs = getMessagesForCurrent();

    if (!currentConv) return;

    // Welcome header
    if (currentConv.type === 'channel') {
        var welcome = document.createElement('div');
        welcome.className = 'ch-welcome';
        welcome.innerHTML =
            '<div class="ch-icon">#</div>' +
            '<h3>Welcome to #' + esc(currentConv.channel) + '</h3>' +
            '<p>All messages in this channel from server "' + esc(currentConv.server) + '"</p>';
        container.appendChild(welcome);
    } else {
        var welcome = document.createElement('div');
        welcome.className = 'ch-welcome';
        welcome.innerHTML =
            '<div class="ch-icon">@</div>' +
            '<h3>' + esc(currentConv.users.join(' \u2194 ')) + '</h3>' +
            '<p>Direct message conversation between these users</p>';
        container.appendChild(welcome);
    }

    if (msgs.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'no-messages';
        empty.innerHTML =
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
                '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>' +
            '</svg>' +
            '<p>No messages yet in this conversation</p>';
        container.appendChild(empty);
        scrollToBottom();
        return;
    }

    for (var i = 0; i < msgs.length; i++) {
        var msg = msgs[i];
        var prev = i > 0 ? msgs[i - 1] : null;
        var isGroupStart = !prev ||
            prev.sender !== msg.sender ||
            (msg.timestamp - prev.timestamp) > MSG_GROUP_GAP;

        var div = document.createElement('div');
        div.className = 'msg-row' + (isGroupStart ? ' msg-start' : '');

        var html = '';
        if (isGroupStart) {
            var color = avatarColor(msg.sender);
            html += '<div class="msg-avatar" style="background:' + color + '">' +
                initial(msg.sender) + '</div>';
            html += '<div class="msg-body">';
            html += '<div class="msg-meta">';
            html += '<span class="msg-sender">' + esc(msg.sender);
            if (msg.sender === currentUser) html += ' <span class="inline-badge">YOU</span>';
            html += '</span>';
            html += '<span class="msg-time">' + formatTime(msg.timestamp) + '</span>';
            html += '</div>';
            html += '<div class="msg-text">' + esc(msg.message) + '</div>';
            html += '</div>';
        } else {
            html += '<span class="msg-hover-ts">' + shortTime(msg.timestamp) + '</span>';
            html += '<div class="msg-body">';
            html += '<div class="msg-text">' + esc(msg.message) + '</div>';
            html += '</div>';
        }

        div.innerHTML = html;
        container.appendChild(div);
    }

    scrollToBottom();
}

function scrollToBottom() {
    var container = document.getElementById('monitorMessages');
    container.scrollTop = container.scrollHeight;
}

// ======= Sending Messages =======

async function sendMonitorMessage() {
    var input = document.getElementById('monitorInput');
    var text = input.value.trim();
    if (!text || !currentConv) return;
    input.value = '';
    input.focus();

    var result;
    if (currentConv.type === 'channel') {
        result = await apiPost('/admin/send-to-channel', {
            server: currentConv.server,
            channel: currentConv.channel,
            message: text,
        });
    } else {
        var otherUser = currentConv.users.find(function(u) { return u !== currentUser; }) || currentConv.users[0];
        result = await apiPost('/admin/send-to-dm', {
            target: otherUser,
            message: text,
        });
    }

    if (result && result.status === 'ok') {
        await fetchAllConversations();
        var msgs = getMessagesForCurrent();
        var key = convKey(currentConv);
        localCache[key] = { messages: msgs, lastCount: msgs.length };
        renderCurrentMessages();
    } else if (result && result.error) {
        showToast(result.error);
    }
}

// ======= Polling =======

function startPolling() {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(function() {
        fetchAllConversations();
    }, POLL_MS);
}

function startHeartbeat() {
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(function() {
        apiPost('/heartbeat', {});
    }, HEARTBEAT_MS);
}

// ======= Search =======

function filterMonitorSidebar() {
    var q = (document.getElementById('monitorSearch') || {}).value;
    if (!q) q = '';
    q = q.toLowerCase();
    document.querySelectorAll('.conv-item').forEach(function(el) {
        var name = el.textContent.toLowerCase();
        el.style.display = name.includes(q) ? '' : 'none';
    });
}

// ======= Panels =======

function toggleUsersPanel() {
    var panel = document.getElementById('usersPanel');
    panel.classList.toggle('hidden');
}

// ======= Logout =======

async function handleMonitorLogout() {
    await apiPost('/logout', {});
    localStorage.removeItem('session_token');
    if (pollingInterval) clearInterval(pollingInterval);
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    window.location.href = '/login';
}

// ======= Switch to chat =======

function switchToChat() {
    window.location.href = '/chat.html';
}

// ======= Start =======

document.addEventListener('DOMContentLoaded', init);
