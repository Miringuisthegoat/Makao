/* =============================================
   Makao — chatbot.js
   Floating chatbot widget — calls Groq via backend
   ============================================= */

const fab      = document.getElementById('chatbotFab');
const drawer   = document.getElementById('chatbotDrawer');
const closeBtn = document.getElementById('drawerClose');
const messages = document.getElementById('drawerMessages');
const input    = document.getElementById('chatInput');
const sendBtn  = document.getElementById('chatSend');

let sessionId    = localStorage.getItem('makao_session') || crypto.randomUUID();
let chatHistory  = [];
localStorage.setItem('makao_session', sessionId);

// ===== TOGGLE DRAWER =====
fab?.addEventListener('click', () => {
  drawer.classList.toggle('open');
  if (drawer.classList.contains('open')) input?.focus();
});
closeBtn?.addEventListener('click', () => drawer.classList.remove('open'));

// ===== SEND SUGGESTION =====
function sendSuggestion(btn) {
  if (!input) return;
  input.value = btn.textContent;
  btn.closest('.chat-suggestions')?.remove();
  sendMessage();
}
window.sendSuggestion = sendSuggestion;

// ===== ADD MESSAGE TO UI =====
function addMessage(text, role) {
  const div = document.createElement('div');
  div.className = `msg ${role === 'user' ? 'user-msg' : 'bot-msg'}`;
  div.innerHTML = `<p>${text.replace(/\n/g, '<br/>')}</p>`;
  messages?.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

// ===== TYPING INDICATOR =====
function showTyping() {
  const div = document.createElement('div');
  div.className = 'msg bot-msg typing-indicator';
  div.id = 'typing';
  div.innerHTML = '<span></span><span></span><span></span>';
  messages?.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}
function hideTyping() {
  document.getElementById('typing')?.remove();
}

// ===== SEND MESSAGE =====
async function sendMessage() {
  const text = input?.value.trim();
  if (!text) return;
  input.value = '';

  addMessage(text, 'user');
  chatHistory.push({ role: 'user', content: text });
  showTyping();

  try {
    const res = await fetch('/api/chatbot/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text, history: chatHistory })
    });

    hideTyping();

    if (!res.ok) throw new Error('API error');
    const data = await res.json();
    const reply = data.reply || 'Samahani, kuna tatizo. / Sorry, something went wrong.';

    addMessage(reply, 'bot');
    chatHistory.push({ role: 'assistant', content: reply });

    // If listings returned, show quick links
    if (data.listings?.length) {
      const linksDiv = document.createElement('div');
      linksDiv.className = 'chat-suggestions';
      data.listings.slice(0, 3).forEach(l => {
        const btn = document.createElement('button');
        btn.className = 'suggestion-chip';
        btn.textContent = `🏠 ${l.title} — KES ${Number(l.price_per_month).toLocaleString('en-KE')}/mo`;
        btn.onclick = () => window.location.href = `listing.html?id=${l.id}`;
        linksDiv.appendChild(btn);
      });
      messages?.appendChild(linksDiv);
      messages.scrollTop = messages.scrollHeight;
    }

  } catch (err) {
    hideTyping();
    addMessage('Samahani, kuna tatizo la mtandao. Tafadhali jaribu tena.\n(Sorry, there was a connection error. Please try again.)', 'bot');
  }
}

// ===== EVENT LISTENERS =====
sendBtn?.addEventListener('click', sendMessage);
input?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});