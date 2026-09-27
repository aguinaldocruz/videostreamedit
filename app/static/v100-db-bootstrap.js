(function () {
  async function api(url, options) {
    const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...(options || {})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`);
    return data;
  }
  function esc(value) { return String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function openWizard() {
    if (document.getElementById('database-bootstrap-dialog')) return;
    const dialog = document.createElement('dialog'); dialog.id = 'database-bootstrap-dialog'; dialog.className = 'database-bootstrap-dialog';
    dialog.innerHTML = `<form method="dialog" class="database-bootstrap-card"><h2>Connect VideoStreamEdit to PostgreSQL</h2><p class="muted">The administrator account is used only to create the application database and user. It is never saved.</p><div class="bootstrap-grid"><label>PostgreSQL URL<input name="server_url" required placeholder="postgresql://db.example.local:5432/postgres"></label><label>Maintenance database<input name="maintenance_database" value="postgres"></label><label>Administrator user<input name="admin_user" required autocomplete="username"></label><label>Administrator password<input name="admin_password" type="password" autocomplete="current-password"></label><label>Target database<input name="target_database" value="videostreamedit" required></label><label>Application user<input name="app_user" value="videostreamedit" required></label><label>Application password<input name="app_password" type="password" required autocomplete="new-password"></label><label>Installation mode<select name="mode"><option value="new">New installation</option><option value="restore">Restore a backup after setup</option></select></label><label class="bootstrap-wide">Backup archive path (optional for restore)<input name="backup_path" placeholder="/backup/videostreamedit-backup-….tar.gz"></label></div><p class="bootstrap-error" hidden></p><div class="dialog-actions"><button type="submit">Configure database</button></div><p class="muted bootstrap-security">The application connection is stored encrypted under /config. Restart the container after setup.</p></form>`;
    document.body.appendChild(dialog);
    dialog.querySelector('form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget, error = form.querySelector('.bootstrap-error'), button = form.querySelector('button[type="submit"]'); error.hidden = true; button.disabled = true; button.textContent = 'Creating database…';
      try { const result = await api('/api/bootstrap/configure', {method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(form).entries()))}); form.innerHTML = `<h2>PostgreSQL is ready</h2><p>${esc(result.message)}</p><div class="dialog-actions"><button type="button" data-bootstrap-close>Close</button></div>`; form.querySelector('[data-bootstrap-close]').onclick = () => dialog.close(); } catch (err) { error.textContent = err.message; error.hidden = false; button.disabled = false; button.textContent = 'Configure database'; }
    });
    dialog.addEventListener('close', () => dialog.remove(), {once: true}); dialog.showModal();
  }
  async function check() { try { const status = await api('/api/bootstrap/status'); if (status.bootstrap) openWizard(); } catch (_) {} }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', check); else check();
})();
