const PORT = 9342;
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const res = await fetch('http://127.0.0.1:' + PORT + '/json/new?' + encodeURIComponent('about:blank'), { method: 'PUT' });
  const tab = await res.json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0;
  const cdp = (m, p) => new Promise((resolve, reject) => {
    const i = ++id;
    const h = ev => { const msg = JSON.parse(ev.data); if (msg.id === i) { ws.removeEventListener('message', h); msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result); } };
    ws.addEventListener('message', h);
    ws.send(JSON.stringify({ id: i, method: m, params: p || {} }));
  });

  await cdp('Page.enable');
  await cdp('Runtime.enable');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp('Page.navigate', { url: 'http://localhost:8086/lamaran' });
  await sleep(2500);

  const evalJs = async (expr) => {
    const r = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return 'EXC: ' + JSON.stringify(r.exceptionDetails).slice(0, 200);
    return r.result.value;
  };

  // 1) ambil kartu pertama di kolom Applied
  const firstCard = await evalJs(`(() => {
    const c = document.querySelector('#col-applied .tcard');
    return c ? { id: c.dataset.id, search: c.dataset.search, stage: c.dataset.stage } : null;
  })()`);
  console.log('kartu pertama applied:', JSON.stringify(firstCard));

  if (!firstCard) { console.log('FAIL: tidak ada kartu'); process.exit(1); }

  // 2) aktifkan global search dengan query yang TIDAK cocok dengan kartu tsb
  //    pakai karakter aneh yang tidak ada di data
  const nonMatch = 'zzzqqqxxx999';
  await evalJs(`(() => {
    const g = document.getElementById('globalSearch');
    g.value = '${nonMatch}';
    g.dispatchEvent(new Event('input'));
  })()`);
  await sleep(400);
  const visibleBefore = await evalJs(`document.querySelectorAll('.tcard:not(.filtered-out)').length`);
  console.log('kartu visible dengan global search (harus 0):', visibleBefore);

  // 3) pindahkan kartu via moveCard (simulasi drop) ke Follow-up
  await evalJs(`moveCard(${firstCard.id}, 'follow_up')`);
  await sleep(700);
  const inFollowUp = await evalJs(`!!document.querySelector('#col-follow_up .tcard[data-id="${firstCard.id}"]')`);
  const filteredAfter = await evalJs(`document.querySelector('.tcard[data-id="${firstCard.id}"]').classList.contains('filtered-out')`);
  console.log('kartu pindah ke Follow-up:', inFollowUp, '| ter-filter (harus true):', filteredAfter);

  // 4) bersihkan global search → kartu harus terlihat lagi di Follow-up
  await evalJs(`(() => { const g = document.getElementById('globalSearch'); g.value = ''; g.dispatchEvent(new Event('input')); })()`);
  await sleep(400);
  const visibleAfter = await evalJs(`document.querySelector('.tcard[data-id="${firstCard.id}"]').classList.contains('filtered-out')`);
  console.log('setelah search dikosongkan, kartu tidak ter-filter (harus true):', !visibleAfter);

  // 5) kembalikan kartu ke Applied biar data bersih
  await evalJs(`moveCard(${firstCard.id}, 'applied')`);
  await sleep(600);
  const backInApplied = await evalJs(`!!document.querySelector('#col-applied .tcard[data-id="${firstCard.id}"]')`);
  console.log('dikembalikan ke Applied:', backInApplied);

  // 6) cek error konsol
  const errs = await cdp('Runtime.evaluate', { expression: 'window.__errs || []', returnByValue: true });
  console.log('console errors:', JSON.stringify(errs.result.value || []));

  ws.close();
  process.exit(0);
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
