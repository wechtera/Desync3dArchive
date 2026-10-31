const form = document.getElementById('login-form');
const errorEl = document.getElementById('login-error');

// A link from another site arrives without the Strict cookie, so a live session lands here.
fetch('/api/session')
  .then((res) => res.json())
  .then((data) => {
    if (data.authenticated) window.location.replace('/');
  })
  .catch(() => {});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.textContent = '';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: document.getElementById('login-password').value }),
    });
    if (res.ok) {
      window.location.href = '/';
      return;
    }
    const data = await res.json();
    errorEl.textContent = data.error || 'Login failed.';
  } catch (err) {
    errorEl.textContent = `Login failed: ${err.message}`;
  }
});
