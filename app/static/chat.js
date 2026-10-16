const messagesContainer = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');

// ============ 发送消息 ============
async function sendMessage() {
    const question = input.value.trim();
    if (!question || input.disabled) return;
    input.disabled = true;
    sendBtn.disabled = true;
    addMessage('user', question);
    input.value = '';
    const bubble = addMessage('bot', '正在生成查询…');
    const answer = bubble.firstElementChild;
    answer.classList.add("is-loading");
    answer.setAttribute("aria-busy", "true");
    let content = '', completed = false, timer = null, reader;
    const redraw = () => { timer = null; renderAnswer(answer, content); };
    const handleEvent = (frame) => {
        let type = 'message';
        const lines = [];
        for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) type = line.slice(6).trim();
            if (line.startsWith('data:')) lines.push(line.slice(5).trimStart());
        }
        if (!lines.length) return;
        const payload = JSON.parse(lines.join('\n'));
        if (type === 'status' && !content) answer.textContent = payload.message;
        if (type === 'delta') {
            content += payload.text;
            if (timer === null) timer = setTimeout(redraw, 80);
        }
        if (type === 'error') throw new Error(payload.message || '生成回答失败');
        if (type === 'done') {
            completed = true;
            clearTimeout(timer);
            redraw();
            let extra = '';
            if (payload.cypher) extra += `<div class="cypher">🔍 ${escapeHtml(payload.cypher)}</div>`;
            if (payload.data && payload.data.length) {
                extra += '<div class="data-toggle" onclick="toggleData(this)">📊 查看原始数据</div>';
                extra += `<div class="raw-data">${escapeHtml(JSON.stringify(payload.data, null, 2))}</div>`;
            }
            bubble.insertAdjacentHTML('beforeend', extra);
        }
    };
    try {
        const response = await fetch('/ask', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, limit: 30 })
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.error || '请求失败');
        }
        if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
            throw new Error('服务未返回事件流，请刷新页面并确认后端已更新');
        }
        reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        while (!completed) {
            const { value, done } = await reader.read();
            buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
            buffer = buffer.replace(/\r\n/g, '\n');
            let boundary;
            while ((boundary = buffer.indexOf('\n\n')) !== -1) {
                const frame = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                handleEvent(frame);
                if (completed) break;
            }
            if (done) break;
        }
        if (!completed) throw new Error('连接中断，回答未完成');
    } catch (error) {
        clearTimeout(timer);
        if (content) renderAnswer(answer, content);
        else answer.textContent = '';
        const notice = document.createElement('div');
        notice.className = 'error';
        notice.textContent = `❌ ${content ? '回答未完成：' : ''}${error.message}`;
        bubble.appendChild(notice);
    } finally {
        answer.classList.remove("is-loading");
        answer.removeAttribute("aria-busy");
        clearTimeout(timer);
        if (reader) {
            try { await reader.cancel(); } catch (_) { /* 连接可能已经关闭 */ }
            reader.releaseLock();
        }
        input.disabled = false;
        sendBtn.disabled = false;
        input.focus();
    }
}

function renderAnswer(element, content) {
    element.classList.remove("is-loading");
    element.removeAttribute("aria-busy");
    if (window.marked && window.DOMPurify) {
        element.className = 'markdown-body';
        element.innerHTML = DOMPurify.sanitize(marked.parse(content, { gfm: true, breaks: false }));
    } else {
        element.textContent = content;
        element.style.whiteSpace = 'pre-wrap';
    }
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// ============ 添加消息 ============
function escapeHtml(text) {
    const span = document.createElement('span');
    span.textContent = String(text);
    return span.innerHTML;
}

function addMessage(role, content, extra = '') {
    const div = document.createElement('div');
    div.className = `message ${role}`;
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const answer = document.createElement('div');
    bubble.appendChild(answer);
    if (role === 'bot') renderAnswer(answer, content);
    else { answer.textContent = content; answer.style.whiteSpace = 'pre-wrap'; }
    bubble.insertAdjacentHTML('beforeend', extra);
    div.appendChild(bubble);
    messagesContainer.appendChild(div);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    return bubble;
}

// ============ 切换显示原始数据 ============
function toggleData(el) {
    const raw = el.nextElementSibling;
    if (raw.style.display === 'block') {
        raw.style.display = 'none';
        el.textContent = '📊 查看原始数据';
    } else {
        raw.style.display = 'block';
        el.textContent = '📊 收起原始数据';
    }
}

// ============ 检查服务状态 ============
async function checkHealth() {
    try {
        const res = await fetch('/health');
        const data = await res.json();
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        if (data.status === 'ok') {
            dot.className = 'status-dot online';
            text.textContent = '在线';
        } else {
            dot.className = 'status-dot offline';
            text.textContent = '离线';
        }
    } catch {
        document.getElementById('statusDot').className = 'status-dot offline';
        document.getElementById('statusText').textContent = '离线';
    }
}

// 页面打开时立即检查，之后每300秒检查一次状态
checkHealth();
setInterval(checkHealth, 300000);
