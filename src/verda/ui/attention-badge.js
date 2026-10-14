// A small link in every page header: how many topics wait for the user. Quiet when there is nothing.
(() => {
  const header = document.querySelector('header');
  if (!header) return;
  const link = document.createElement('a');
  link.href = '/control/attention'; link.textContent = 'Bekleyen konular'; link.style.cssText = 'margin-left:14px;font-size:13px;text-decoration:none;color:inherit';
  header.appendChild(link);
  fetch('/control/private/attention', {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    link.textContent = d.count ? 'Bekleyen konular · ' + d.count : 'Bekleyen konu yok';
    if (d.count) { link.style.fontWeight = '650'; link.style.color = 'var(--bad, #b3261e)'; }
  }).catch(() => {});
})();
