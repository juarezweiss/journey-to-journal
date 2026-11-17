(() => {
  if (location.origin !== __JOURNEY_ORIGIN__) throw new Error('Run only on your Journey tab.');
  window.__journeyExportCleanup?.();
  const url = __LOCAL_URL__;
  const localOrigin = new URL(url).origin;
  const popup = window.open(url, '_blank', 'popup,width=500,height=200');
  if (!popup) throw new Error('Allow the local authentication popup and retry.');
  let ready = false, sent = false, candidate;
  const originalFetch = window.fetch;
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalHeader = XMLHttpRequest.prototype.setRequestHeader;
  const urls = new WeakMap();
  const isDrive = value => {
    try {
      const u = new URL(value, location.href);
      return u.protocol === 'https:' &&
        (u.hostname === 'www.googleapis.com' || u.hostname === 'content.googleapis.com') &&
        /^\/drive\/v[23]\//.test(u.pathname);
    } catch { return false; }
  };
  const send = () => {
    if (ready && candidate && !sent) {
      popup.postMessage({type:'journey-export-token', token:candidate}, localOrigin);
      candidate = undefined;
      sent = true;
    }
  };
  const capture = value => {
    if (sent) return;
    if (typeof value === 'string' && /^Bearer\s+\S+$/i.test(value)) {
      candidate = value.replace(/^Bearer\s+/i, '');
      send();
    }
  };
  const fetchHook = function(input, init) {
    if (isDrive(input instanceof Request ? input.url : input)) {
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
      capture(headers.get('Authorization'));
    }
    return originalFetch.apply(this, arguments);
  };
  const openHook = function(method, target) {
    urls.set(this, target);
    return originalOpen.apply(this, arguments);
  };
  const headerHook = function(name, value) {
    if (name.toLowerCase() === 'authorization' && isDrive(urls.get(this))) capture(value);
    return originalHeader.apply(this, arguments);
  };
  const cleanup = () => {
    if (window.fetch === fetchHook) window.fetch = originalFetch;
    if (XMLHttpRequest.prototype.open === openHook) XMLHttpRequest.prototype.open = originalOpen;
    if (XMLHttpRequest.prototype.setRequestHeader === headerHook) XMLHttpRequest.prototype.setRequestHeader = originalHeader;
    window.removeEventListener('message', onMessage);
    clearInterval(timer);
    clearTimeout(expiry);
    candidate = undefined;
    if (window.__journeyExportCleanup === cleanup) delete window.__journeyExportCleanup;
  };
  const onMessage = e => {
    if (e.source !== popup || e.origin !== localOrigin) return;
    if (e.data === 'journey-export-ready') { ready = true; send(); }
    if (e.data === 'journey-export-received') cleanup();
  };
  window.addEventListener('message', onMessage);
  window.__journeyExportCleanup = cleanup;
  window.fetch = fetchHook;
  XMLHttpRequest.prototype.open = openHook;
  XMLHttpRequest.prototype.setRequestHeader = headerHook;
  const timer = setInterval(() => {
    try {
      // Journey's current public web app uses the Pinia "main" store.
      // Read only its existing Drive credential cache; never enumerate journal state.
      const app = document.querySelector('#__nuxt')?.__vue_app__;
      const main = app?.config?.globalProperties?.$pinia?.state?.value?.main;
      const tokens = Object.values(main?.googleDriveAccessTokens ?? {})
        .map(item => item?.accessToken).filter(item => typeof item === 'string' && item.length);
      const unique = [...new Set(tokens)];
      const t = window.gapi?.client?.getToken?.();
      const state = unique.length === 1 || t?.access_token ? 'found' :
        unique.length > 1 ? 'multiple' : !app ? 'no-app' : !main ? 'no-pinia' : 'empty';
      if (ready && !sent) popup.postMessage({type:'journey-export-status', state}, localOrigin);
      if (t?.access_token) capture('Bearer ' + t.access_token);
      else if (unique.length === 1) capture('Bearer ' + unique[0]);
    } catch { /* No credential output. */ }
  }, 500);
  const expiry = setTimeout(cleanup, 180000);
  // If needed, open an existing entry after running this. Do not edit or sync.
})();
