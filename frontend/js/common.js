/* TransmiEye · utilidades compartidas por todas las páginas */
const TE = (() => {
  const LS_TOKEN = 'te_token', LS_USR = 'te_usuario';
  const token = () => localStorage.getItem(LS_TOKEN);
  const usuario = () => JSON.parse(localStorage.getItem(LS_USR) || 'null');
  const nivelRol = { visor: 1, supervisor: 2, admin: 3 };
  const puede = (rol) => { const u = usuario(); return !!u && nivelRol[u.rol] >= nivelRol[rol]; };

  const TIPOS = {
    INGRESO_LATERAL: 'Ingreso por puerta lateral',
    PERSONA_CARRIL: 'Persona en el carril',
    SALIDA_LATERAL: 'Salida por puerta lateral',
    AGLOMERACION: 'Aglomeración en puertas',
  };
  const NIVELES = { critica: 'Crítica', media: 'Media', baja: 'Baja' };

  function salir(motivo) {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_USR);
    location.href = 'index.html' + (motivo ? '?motivo=' + encodeURIComponent(motivo) : '');
  }

  async function api(metodo, url, cuerpo) {
    const op = { method: metodo, headers: { Authorization: 'Bearer ' + token() } };
    if (cuerpo instanceof FormData) op.body = cuerpo;
    else if (cuerpo !== undefined) { op.headers['Content-Type'] = 'application/json'; op.body = JSON.stringify(cuerpo); }
    let r;
    try { r = await fetch(url, op); }
    catch (e) { throw new Error('No hay conexión con el servidor de TransmiEye.'); }
    if (r.status === 401) { salir('La sesión expiró. Ingresa de nuevo.'); throw new Error('Sesión expirada'); }
    const tipo = r.headers.get('content-type') || '';
    const datos = tipo.includes('json') ? await r.json() : await r.text();
    if (!r.ok) {
      const det = datos && datos.detail;
      throw new Error(typeof det === 'string' ? det : Array.isArray(det) ? det.map(d => d.msg).join('; ') : `Error ${r.status}`);
    }
    return datos;
  }
  const get = (u) => api('GET', u);
  const conToken = (url) => url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token());

  // ── formato ───────────────────────────────────────────────────
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fecha = (iso) => iso ? new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '–';
  const hora = (iso) => iso ? new Date(iso).toLocaleTimeString('es-CO') : '–';
  const pct = (x) => x == null ? '–' : `${Math.round(x * 100)} %`;
  const hace = (iso) => {
    const s = Math.round((Date.now() - new Date(iso)) / 1000);
    if (s < 60) return `hace ${s} s`;
    if (s < 3600) return `hace ${Math.round(s / 60)} min`;
    if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
    return fecha(iso);
  };
  const etiquetaNivel = (n) => n ? `<span class="etq ${n}">${NIVELES[n] || n}</span>` : '';
  const etiquetaRevision = (c) => c === true ? '<span class="etq verde">Confirmado</span>'
    : c === false ? '<span class="etq gris">Rechazado</span>' : '<span class="etq ambar">Pendiente</span>';
  const nivelDeRiesgo = (r) => r >= 5 ? 'critica' : r >= 3 ? 'media' : 'baja';

  // ── navegación ────────────────────────────────────────────────
  const PAGINAS = [
    ['panel.html', 'Panel'], ['camaras.html', 'Cámaras'], ['alertas.html', 'Alertas'],
    ['historial.html', 'Historial'], ['estaciones.html', 'Estaciones'], ['reportes.html', 'Reportes'],
    ['configuracion.html', 'Configuración'],
  ];
  const LOGO = `<svg width="38" height="38" viewBox="0 0 38 38" aria-hidden="true">
      <rect width="38" height="38" rx="8" fill="#B3202A"/>
      <path d="M5 19c4-6.5 8.7-9.7 14-9.7S29 12.5 33 19c-4 6.5-8.7 9.7-14 9.7S9 25.5 5 19z" fill="#fff"/>
      <circle cx="19" cy="19" r="5.6" fill="#26313B"/><circle cx="21" cy="17" r="1.7" fill="#fff"/></svg>`;

  function montar(activa) {
    if (!token()) { location.href = 'index.html'; return false; }
    const u = usuario();
    const ruta = document.createElement('aside');
    ruta.className = 'ruta';
    ruta.innerHTML = `
      <div class="ruta-marca">${LOGO}<div><strong>TransmiEye</strong><span>Evasión en puertas laterales</span></div></div>
      <nav aria-label="Secciones">${PAGINAS.map(([href, nombre]) =>
        `<a class="parada ${href === activa ? 'activa' : ''}" href="${href}" ${href === activa ? 'aria-current="page"' : ''}>
           <span>${nombre}</span>${href === 'alertas.html' ? '<span class="contador oculto" id="cnt-alertas">0</span>' : ''}</a>`).join('')}
      </nav>
      <div class="ruta-pie">
        <div class="conexion" id="conexion"><i></i><span>Conectando…</span></div>
        <div class="quien">${esc(u.nombre)}</div>
        <div class="rol">${{ admin: 'Administrador', supervisor: 'Supervisor', visor: 'Visor' }[u.rol]}</div>
        <button type="button" id="btn-salir">Cerrar sesión</button>
      </div>`;
    document.body.prepend(ruta);
    if (!document.getElementById('toasts')) {
      const t = document.createElement('div'); t.id = 'toasts'; t.setAttribute('aria-live', 'polite'); document.body.append(t);
    }
    document.getElementById('btn-salir').onclick = () => salir();
    document.querySelectorAll('[data-rol]').forEach(el => { if (!puede(el.dataset.rol)) el.classList.add('oculto'); });
    conectarWS();
    actualizarContador();
    get('/api/config').then(c => { sonidoActivo = c.ajustes.sonido_alertas; }).catch(() => {});
    return true;
  }

  async function actualizarContador() {
    try {
      const d = await get('/api/alertas?limite=1');
      const n = Object.values(d.activas).reduce((a, b) => a + b, 0);
      const el = document.getElementById('cnt-alertas');
      if (el) { el.textContent = n; el.classList.toggle('oculto', n === 0); }
    } catch (e) { /* ignorado */ }
  }

  // ── alertas en tiempo real ─────────────────────────────────────
  let ws, espera = 1000, sonidoActivo = true, audio;
  document.addEventListener('click', () => { try { audio = audio || new AudioContext(); audio.resume(); } catch (e) {} }, { once: true });

  function pitido(critica) {
    if (!sonidoActivo) return;
    try {
      audio = audio || new AudioContext();
      const tonos = critica ? [880, 660, 880] : [660];
      tonos.forEach((f, i) => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.frequency.value = f; o.type = 'square';
        g.gain.setValueAtTime(0.07, audio.currentTime + i * 0.18);
        g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + i * 0.18 + 0.16);
        o.connect(g).connect(audio.destination);
        o.start(audio.currentTime + i * 0.18); o.stop(audio.currentTime + i * 0.18 + 0.17);
      });
    } catch (e) { /* sin audio */ }
  }

  function estadoConexion(ok, texto) {
    const c = document.getElementById('conexion');
    if (!c) return;
    c.className = 'conexion ' + (ok ? 'ok' : 'mal');
    c.querySelector('span').textContent = texto;
  }

  function conectarWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token())}`);
    ws.onopen = () => { espera = 1000; estadoConexion(true, 'En vivo'); };
    ws.onclose = (e) => {
      if (e.code === 4401) return salir('La sesión expiró. Ingresa de nuevo.');
      estadoConexion(false, 'Sin conexión, reintentando');
      setTimeout(conectarWS, espera); espera = Math.min(espera * 2, 15000);
    };
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      document.dispatchEvent(new CustomEvent('te:' + d.tipo, { detail: d }));
      if (d.tipo === 'evento' && d.alerta_id) {
        toast(`<b>${esc(d.nombre_tipo)}</b>${esc(d.estacion)} · ${esc(d.camara)} · ${hora(d.timestamp)}
               <br><button type="button" data-ev="${d.evento_id}">Ver evidencia</button>`, d.nivel === 'critica' ? 'critica' : '', 9000);
        pitido(d.nivel === 'critica');
      }
      if (['evento', 'revision', 'alerta_cerrada'].includes(d.tipo)) actualizarContador();
    };
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send('ping'); }, 25000);

  function toast(html, clase = '', ms = 4000) {
    const t = document.createElement('div');
    t.className = 'toast ' + clase;
    t.innerHTML = html;
    const cont = document.getElementById('toasts');
    cont.append(t);
    setTimeout(() => { t.remove(); resumenToasts(); }, ms);
    // En ráfagas se muestran solo las 3 más recientes y un resumen con enlace a Alertas
    const visibles = [...cont.querySelectorAll('.toast:not(.resumen)')];
    visibles.slice(0, Math.max(0, visibles.length - 3)).forEach(v => { v.remove(); ocultas++; });
    resumenToasts();
  }
  let ocultas = 0;
  function resumenToasts() {
    const cont = document.getElementById('toasts');
    let r = cont.querySelector('.resumen');
    if (!ocultas) { r?.remove(); return; }
    if (!r) { r = document.createElement('div'); r.className = 'toast resumen'; cont.prepend(r); }
    r.innerHTML = `<b>${ocultas} aviso${ocultas === 1 ? '' : 's'} más</b><a href="alertas.html" style="color:#fff">Ver todas las alertas</a>
      <button type="button" style="margin-left:8px">Descartar</button>`;
    r.querySelector('button').onclick = () => { ocultas = 0; r.remove(); };
  }
  const error = (e) => toast(`<b>No se pudo completar</b>${esc(e.message || e)}`, 'critica', 6000);

  // ── modal ─────────────────────────────────────────────────────
  function modal(titulo, cuerpo, { pie = '', ancho = false, alCerrar } = {}) {
    const velo = document.createElement('div');
    velo.className = 'velo';
    velo.innerHTML = `<div class="modal ${ancho ? 'ancho' : ''}" role="dialog" aria-modal="true" aria-label="${esc(titulo)}">
      <div class="modal-cab"><h2>${esc(titulo)}</h2><button class="cerrar" type="button" aria-label="Cerrar">×</button></div>
      <div class="modal-cuerpo">${cuerpo}</div>${pie ? `<div class="modal-pie">${pie}</div>` : ''}</div>`;
    const cerrar = () => { velo.remove(); document.removeEventListener('keydown', tecla); alCerrar && alCerrar(); };
    const tecla = (e) => { if (e.key === 'Escape') cerrar(); };
    velo.querySelector('.cerrar').onclick = cerrar;
    velo.addEventListener('mousedown', (e) => { if (e.target === velo) cerrar(); });
    document.addEventListener('keydown', tecla);
    document.body.append(velo);
    velo.querySelector('input, select, textarea, button.btn')?.focus();
    return { el: velo, cerrar, $: (s) => velo.querySelector(s) };
  }

  // ── detalle de evento (usado en panel, alertas e historial) ───
  async function verEvento(id, alCambiar) {
    let e;
    try { e = await get(`/api/eventos/${id}`); } catch (err) { return error(err); }
    const visor = e.tiene_imagen ? `
      <div class="visor">
        <img id="ev-img" src="${conToken(`/api/eventos/${e.id}/imagen`)}" alt="Evidencia anonimizada del evento ${e.id}">
        ${e.clip_frames ? `<div class="visor-controles">
          <button type="button" id="ev-play">Reproducir clip</button>
          <input type="range" id="ev-pos" min="0" max="${e.clip_frames}" value="0" aria-label="Posición del clip">
          <span id="ev-cuadro">Imagen del evento</span></div>` : ''}
      </div>` : `<div class="banda ambar">La imagen se eliminó al cumplirse el periodo de retención. Los datos del evento se conservan.</div>`;
    const puedeRevisar = puede('supervisor');
    const m = modal(`Evento #${e.id} · ${e.tipo_nombre}`, `
      <div class="detalle-evento">
        <div>${visor}</div>
        <div>
          <dl class="ficha">
            <dt>Fecha</dt><dd>${new Date(e.timestamp).toLocaleString('es-CO')}</dd>
            <dt>Estación</dt><dd>${esc(e.estacion)}</dd>
            <dt>Cámara</dt><dd>${esc(e.camara)}</dd>
            <dt>Riesgo</dt><dd>${etiquetaNivel(nivelDeRiesgo(e.nivel_riesgo))}</dd>
            <dt>Confianza</dt><dd>${pct(e.confianza)}</dd>
            <dt>Persona</dt><dd>${e.track_id != null ? '#' + e.track_id : '–'}</dd>
            <dt>Detalle</dt><dd>${esc(e.detalle || '–')}</dd>
            <dt>Revisión</dt><dd>${etiquetaRevision(e.confirmado)} ${e.revisor ? 'por ' + esc(e.revisor) : ''}</dd>
            <dt>SHA-256</dt><dd class="hash">${esc(e.hash_sha256 || '–')}</dd>
          </dl>
          ${puedeRevisar ? `<div class="campo" style="margin-top:16px"><label for="ev-notas">Notas de revisión</label>
            <textarea id="ev-notas" rows="3" placeholder="Qué se observa en el clip">${esc(e.notas || '')}</textarea></div>` : ''}
        </div>
      </div>`, {
      ancho: true,
      pie: `${puedeRevisar && e.tiene_imagen ? `<a class="btn" href="${conToken(`/api/eventos/${e.id}/evidencia.zip`)}">Exportar evidencia</a>` : ''}
            ${puedeRevisar ? `<button class="btn" data-d="rechazar">Rechazar: falso positivo</button>
            <button class="btn confirmar" data-d="confirmar">Confirmar evasión</button>` : ''}`,
    });
    // Reproductor de clip cuadro a cuadro
    if (e.clip_frames) {
      const img = m.$('#ev-img'), pos = m.$('#ev-pos'), lbl = m.$('#ev-cuadro'), btn = m.$('#ev-play');
      const original = img.src; let timer = null;
      const mostrar = (n) => {
        pos.value = n;
        img.src = n === 0 ? original : conToken(`/api/eventos/${e.id}/clip/${n}`);
        lbl.textContent = n === 0 ? 'Imagen del evento' : `Cuadro ${n} de ${e.clip_frames}`;
      };
      for (let i = 1; i <= e.clip_frames; i++) new Image().src = conToken(`/api/eventos/${e.id}/clip/${i}`);   // precarga
      pos.oninput = () => { clearInterval(timer); timer = null; btn.textContent = 'Reproducir clip'; mostrar(+pos.value); };
      btn.onclick = () => {
        if (timer) { clearInterval(timer); timer = null; btn.textContent = 'Reproducir clip'; return; }
        btn.textContent = 'Pausar';
        let n = +pos.value >= e.clip_frames ? 0 : +pos.value;
        timer = setInterval(() => { n++; if (n > e.clip_frames) { clearInterval(timer); timer = null; btn.textContent = 'Reproducir clip'; mostrar(0); return; } mostrar(n); }, 1000 / 6);
      };
    }
    m.el.querySelectorAll('[data-d]').forEach(b => b.onclick = async () => {
      try {
        await api('POST', `/api/eventos/${e.id}/revision`, { decision: b.dataset.d, notas: m.$('#ev-notas').value || null });
        toast(b.dataset.d === 'confirmar' ? 'Evasión confirmada.' : 'Evento rechazado como falso positivo.');
        m.cerrar(); alCambiar && alCambiar();
      } catch (err) { error(err); }
    });
  }
  document.addEventListener('click', (ev) => {   // toasts creados antes de cargar la página
    const b = ev.target.closest('#toasts [data-ev]'); if (b) verEvento(+b.dataset.ev);
  });

  // ── gráfica de barras SVG ─────────────────────────────────────
  function barras(el, valores, etiquetas, { destacar = -1, alto = 180, cadaEtiqueta = 1, titulo = '', ancho = 720 } = {}) {
    const margen = 26, base = alto - 22, max = Math.max(1, ...valores);
    const paso = (ancho - margen) / valores.length, bw = Math.max(paso * 0.68, 2);
    const marcas = max >= 2 ? [0, 0.5, 1] : [0, 1];
    const lineas = marcas.map(f => {
      const y = base - f * (base - 10), v = Math.round(max * f);
      return `<line x1="${margen}" x2="${ancho}" y1="${y}" y2="${y}" stroke="#E3E7EA"/><text x="0" y="${y + 4}">${v}</text>`;
    }).join('');
    el.innerHTML = `<svg viewBox="0 0 ${ancho} ${alto}" role="img" aria-label="${esc(titulo)}">${lineas}
      ${valores.map((v, i) => {
        const h = (v / max) * (base - 10), x = margen + i * paso + (paso - bw) / 2;
        return `<rect class="barra ${i === destacar ? 'destacada' : ''}" x="${x}" y="${base - h}" width="${bw}" height="${Math.max(h, v ? 2 : 0)}" rx="2"><title>${esc(etiquetas[i])}: ${v}</title></rect>
          ${i % cadaEtiqueta === 0 ? `<text x="${x + bw / 2}" y="${alto - 6}" text-anchor="middle">${esc(etiquetas[i])}</text>` : ''}`;
      }).join('')}</svg>`;
  }

  function descargar(url) { const a = document.createElement('a'); a.href = conToken(url); a.click(); }

  return { token, usuario, puede, api, get, conToken, esc, fecha, hora, pct, hace, etiquetaNivel, etiquetaRevision,
           nivelDeRiesgo, TIPOS, montar, toast, error, modal, verEvento, barras, descargar, actualizarContador };
})();
