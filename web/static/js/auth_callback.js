/**
 * Finishes the MMF implicit sign-in. The token arrives in the URL fragment, which browsers never
 * send to a server, so this page reads it and posts it to the app.
 */
const params = new URLSearchParams(window.location.hash.substring(1) || window.location.search.substring(1));
const title = document.getElementById('status-title');
const description = document.getElementById('status-desc');
const spinner = document.getElementById('spinner');

function fail(heading, message) {
  spinner.style.display = 'none';
  title.className = 'error';
  title.textContent = heading;
  description.textContent = message;
}

async function finishSignIn() {
  const error = params.get('error') || params.get('error_description');
  if (error) {
    fail('Authorization Denied', error);
    return;
  }

  const accessToken = params.get('access_token');
  if (!accessToken) {
    fail('Token Missing', 'No access token was returned by MyMiniFactory.');
    return;
  }

  try {
    const res = await fetch('/api/auth/token_callback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        access_token: accessToken,
        expires_in: Number(params.get('expires_in')) || 21600,
        state: params.get('state'),
      }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to save token');

    spinner.style.display = 'none';
    title.textContent = 'Connected Successfully!';
    description.textContent = `Connected as @${data.username || 'User'}. Redirecting...`;
    setTimeout(() => { window.location.href = '/#settings'; }, 1200);
  } catch (err) {
    fail('Connection Error', err.message);
  }
}

finishSignIn();
