#!/usr/bin/env python3
# /opt/kw_tester_app.py  â€”  KW Mobile Tester v2

from flask import Flask, render_template_string, request, jsonify, Response
from playwright.async_api import async_playwright
import asyncio, threading, json, time, random, string

app = Flask(__name__)

def _find_chromium():
    import os, subprocess
    if os.environ.get('CHROMIUM_BIN'):
        return os.environ['CHROMIUM_BIN']
    try:
        r = subprocess.run(
            ['python3', '-c',
             'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()'],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    for path in [
        '/usr/lib64/chromium-browser/chromium-browser',
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/snap/bin/chromium',
    ]:
        if os.path.exists(path):
            return path
    return 'chromium-browser'

CHROMIUM_BIN = _find_chromium()

# ===== State =====
_state = {'running': False, 'stop': False}
_stats = {
    'ok': 0, 'err': 0, 'active': 0, 'total': 0, 't0': 0.0,
    'log': [], 'codes': {}, 'times': [], 'devices': {}, 'rps_hist': [],
}
_lock = threading.Lock()

def parse_proxy(raw):
    if not raw:
        return None
    raw = raw.strip()
    scheme, rest = raw.split('://', 1) if '://' in raw else ('http', raw)
    if '@' in rest:
        creds, hostpart = rest.rsplit('@', 1)
        username, password = creds.rsplit(':', 1) if ':' in creds else (creds, '')
    else:
        hostpart = rest
        username = password = ''
    proxy = {'server': f'{scheme}://{hostpart}'}
    if username: proxy['username'] = username
    if password: proxy['password'] = password
    return proxy

def reset_stats(total):
    with _lock:
        _stats.update({'ok':0,'err':0,'active':0,'total':total,'t0':time.time(),
                       'log':[],'codes':{},'times':[],'devices':{},'rps_hist':[]})

def add_log(msg):
    with _lock:
        _stats['log'].append(msg)
        if len(_stats['log']) > 300:
            _stats['log'].pop(0)

# ===== Devices & constants =====
DEVICES = [
    {'ua':'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1','vw':390,'vh':844,'dpr':3.0,'name':'iPhone 15'},
    {'ua':'Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36','vw':412,'vh':915,'dpr':3.5,'name':'Pixel 8 Pro'},
    {'ua':'Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36','vw':360,'vh':780,'dpr':3.0,'name':'Samsung S23'},
    {'ua':'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.153 Mobile/15E148 Safari/604.1','vw':375,'vh':812,'dpr':2.0,'name':'iPhone 12'},
    {'ua':'Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36','vw':393,'vh':851,'dpr':2.75,'name':'Redmi Note 11'},
    {'ua':'Mozilla/5.0 (Linux; Android 13; OPPO Reno8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36','vw':412,'vh':892,'dpr':2.625,'name':'OPPO Reno8'},
    {'ua':'Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36','vw':360,'vh':800,'dpr':2.0,'name':'Samsung A54'},
    {'ua':'Mozilla/5.0 (Linux; Android 13; 2201116TG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36','vw':393,'vh':873,'dpr':2.75,'name':'Xiaomi 12T'},
    {'ua':'Mozilla/5.0 (Linux; Android 12; vivo V23 5G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36','vw':392,'vh':848,'dpr':3.0,'name':'Vivo V23'},
    {'ua':'Mozilla/5.0 (Linux; Android 12; Huawei P50 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.88 Mobile HuaweiBrowser/13.0 Safari/537.36','vw':360,'vh':780,'dpr':3.0,'name':'Huawei P50'},
    {'ua':'Mozilla/5.0 (Linux; Android 14; SM-F946B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36','vw':390,'vh':882,'dpr':2.0,'name':'Galaxy Z Fold5'},
    {'ua':'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1','vw':430,'vh':932,'dpr':3.0,'name':'iPhone 15 Pro Max'},
]

LOCALES   = ['ar-EG','ar-SA','ar-AE','ar-JO','ar-MA']

# ÙƒÙ„ Ù…ØµØ¯Ø±: (referer, utm_source, utm_medium, utm_campaign_prefix)
TRAFFIC_SOURCES = [
    # Google organic
    ('https://www.google.com/search?q=ØªØ·Ø¨ÙŠÙ‚+ØªØ³ÙˆÙŠÙ‚+Ø³ÙˆØ´ÙŠØ§Ù„+Ù…ÙŠØ¯ÙŠØ§',  'google', 'organic', 'search'),
    ('https://www.google.com/search?q=social+media+management',     'google', 'organic', 'search'),
    ('https://www.google.com/search?q=Ø§Ø¯Ø§Ø±Ø©+ØµÙØ­Ø§Øª+ÙÙŠØ³Ø¨ÙˆÙƒ',         'google', 'organic', 'search'),
    ('https://www.google.com/search?q=best+social+media+tool',      'google', 'organic', 'search'),
    # Google CPC
    ('https://www.google.com/',                             'google',    'cpc',       'ads'),
    # Facebook
    ('https://www.facebook.com/',                           'facebook',  'social',    'fb'),
    ('https://l.facebook.com/',                             'facebook',  'social',    'fb'),
    ('https://m.facebook.com/',                             'facebook',  'social',    'fb'),
    # Instagram
    ('https://www.instagram.com/',                          'instagram', 'social',    'ig'),
    ('https://l.instagram.com/',                            'instagram', 'social',    'ig'),
    # Twitter/X
    ('https://t.co/',                                       'twitter',   'social',    'tw'),
    ('https://x.com/',                                      'twitter',   'social',    'tw'),
    # YouTube
    ('https://www.youtube.com/',                            'youtube',   'social',    'yt'),
    # WhatsApp
    ('https://web.whatsapp.com/',                           'whatsapp',  'social',    'wa'),
    # Direct (Ø¨Ø¯ÙˆÙ† referer)
    ('',                                                    '',          '',          ''),
    ('',                                                    '',          '',          ''),
]

UTM_CONTENTS = ['banner','story','reel','post','feed','link','bio']

def _build_url(base_url, traffic_mix):
    """ÙŠØ¶ÙŠÙ UTM Ø¹Ø´ÙˆØ§Ø¦ÙŠ ÙˆÙŠØ±Ø¬Ø¹ (final_url, referer)"""
    if not traffic_mix:
        return base_url, random.choice(['https://www.google.com/', ''])
    src = random.choice(TRAFFIC_SOURCES)
    referer, utm_src, utm_med, utm_camp = src
    if not utm_src:
        return base_url, ''
    content  = random.choice(UTM_CONTENTS)
    sep      = '&' if '?' in base_url else '?'
    final    = (f'{base_url}{sep}utm_source={utm_src}'
                f'&utm_medium={utm_med}'
                f'&utm_campaign={utm_camp}'
                f'&utm_content={content}')
    return final, referer
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
Object.defineProperty(navigator,'languages',{get:()=>['ar-EG','ar','en-US','en']});
Object.defineProperty(navigator,'platform',{get:()=>'Linux aarch64'});
window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
const _orig=window.navigator.permissions.query;
window.navigator.permissions.query=(p)=>p.name==='notifications'?Promise.resolve({state:'denied'}):_orig(p);
"""

# ===== Human simulation helpers =====
async def _human_move(page, tx, ty):
    """ØªØ­Ø±ÙŠÙƒ Ù…ÙˆØ³ ØªØ¯Ø±ÙŠØ¬ÙŠ Ø¨Ù…Ù†Ø­Ù†Ù‰ smoothstep + Ø§Ø±ØªØ¬Ø§Ø¬ Ø¹Ø´ÙˆØ§Ø¦ÙŠ"""
    sx = random.uniform(30, 360)
    sy = random.uniform(80, 600)
    steps = random.randint(7, 16)
    for i in range(1, steps + 1):
        t    = i / steps
        ease = t * t * (3 - 2 * t)           # smoothstep
        nx   = sx + (tx - sx) * ease + random.gauss(0, 1.5)
        ny   = sy + (ty - sy) * ease + random.gauss(0, 1.5)
        await page.mouse.move(nx, ny)
        await asyncio.sleep(random.uniform(0.007, 0.032))

async def _human_scroll(page, px):
    """ØªÙ…Ø±ÙŠØ± ØªØ¯Ø±ÙŠØ¬ÙŠ â€” Ù„Ø£Ø¹Ù„Ù‰ Ø£Ùˆ Ø£Ø³ÙÙ„"""
    steps  = max(2, abs(px) // 70)
    per    = px // steps
    for _ in range(steps):
        await page.evaluate(f'window.scrollBy(0, {per})')
        await asyncio.sleep(random.uniform(0.05, 0.18))

async def _get_clickables(page):
    """Ø£ÙˆÙ„ 50 Ø¹Ù†ØµØ± Ù‚Ø§Ø¨Ù„ Ù„Ù„Ø¶ØºØ· ÙˆØ¸Ø§Ù‡Ø± Ù…Ø¹ Ø¥Ø­Ø¯Ø§Ø«ÙŠØ§ØªÙ‡"""
    sel = 'a[href], button, [role="button"], .btn, input[type="submit"], input[type="button"], label, li, [onclick]'
    els = await page.query_selector_all(sel)
    vis = []
    for el in els[:50]:
        try:
            if not await el.is_visible():
                continue
            b = await el.bounding_box()
            if b and b['width'] > 6 and b['height'] > 6:
                vis.append((el, b))
        except Exception:
            pass
    return vis

# ===== Browser session =====
async def run_session(playwright, url, proxy, duration, sid, jitter, traffic_mix=True):
    if jitter > 0:
        await asyncio.sleep(random.uniform(0, jitter))

    dev       = random.choice(DEVICES)
    locale    = random.choice(LOCALES)
    final_url, ref = _build_url(url, traffic_mix)

    launch = {
        'headless': True,
        'executable_path': CHROMIUM_BIN,
        'args': ['--no-sandbox','--disable-setuid-sandbox',
                 '--disable-blink-features=AutomationControlled',
                 '--disable-dev-shm-usage','--disable-gpu','--no-zygote',
                 f'--window-size={dev["vw"]},{dev["vh"]}'],
    }
    proxy_cfg = parse_proxy(proxy)
    if proxy_cfg:
        launch['proxy'] = proxy_cfg

    browser = None
    t_start = time.time()
    nav_ms  = 0

    try:
        with _lock:
            _stats['active'] += 1
            _stats['devices'][dev['name']] = _stats['devices'].get(dev['name'], 0) + 1

        browser = await playwright.chromium.launch(**launch)
        hdrs    = {'Accept-Language': f'{locale},{locale[:2]};q=0.9,en;q=0.7'}
        if ref:
            hdrs['Referer'] = ref

        context = await browser.new_context(
            user_agent=dev['ua'],
            viewport={'width':dev['vw'],'height':dev['vh']},
            device_scale_factor=dev['dpr'],
            is_mobile=True, has_touch=True,
            locale=locale, timezone_id='Africa/Cairo',
            extra_http_headers=hdrs,
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        t_nav = time.time()
        resp  = await page.goto(final_url, wait_until='domcontentloaded', timeout=25000)
        nav_ms = int((time.time() - t_nav) * 1000)

        if resp:
            code = str(resp.status)
            with _lock:
                _stats['codes'][code] = _stats['codes'].get(code, 0) + 1
                _stats['times'].append(nav_ms)
                if len(_stats['times']) > 500:
                    _stats['times'].pop(0)

        # === ÙˆÙ‚Øª Ø§Ù„Ø§Ø³ØªÙŠØ¹Ø§Ø¨ Ø§Ù„Ø£ÙˆÙ„ÙŠ â€” Ø¥Ù†Ø³Ø§Ù† ÙŠØ´ÙˆÙ Ø§Ù„ØµÙØ­Ø© Ø£ÙˆÙ„ Ù…Ø§ ØªÙØªØ­ ===
        await asyncio.sleep(random.uniform(0.8, 2.2))

        # === Ø­Ù„Ù‚Ø© Ø§Ù„Ø£ÙØ¹Ø§Ù„ Ø§Ù„Ø¨Ø´Ø±ÙŠØ© ===
        # Ø§Ù„Ø£ÙˆØ²Ø§Ù†: ØªÙ…Ø±ÙŠØ± Ù„Ø£Ø³ÙÙ„ Ø£ÙƒØ«Ø± Ø´ÙŠØ¡ØŒ Ø«Ù… Ø¶ØºØ·ØŒ Ø«Ù… Ø­Ø±ÙƒØ© Ù…ÙˆØ³ØŒ Ø«Ù… ØªÙˆÙ‚Ù Ù‚Ø±Ø§Ø¡Ø©ØŒ Ø«Ù… hoverØŒ Ø«Ù… ØªÙ…Ø±ÙŠØ± Ù„Ø£Ø¹Ù„Ù‰
        ACTIONS = (
            ['scroll_dn'] * 4 +
            ['click']     * 3 +
            ['move']      * 2 +
            ['pause']     * 2 +
            ['hover']     * 1 +
            ['scroll_up'] * 1
        )

        vw = await page.evaluate('window.innerWidth')
        vh = await page.evaluate('window.innerHeight')

        while (time.time() - t_start) < duration and not _state['stop']:
            action = random.choice(ACTIONS)

            # â€” ØªÙ…Ø±ÙŠØ± Ù„Ø£Ø³ÙÙ„ â€”
            if action == 'scroll_dn':
                await _human_scroll(page, random.randint(100, 420))
                await asyncio.sleep(random.uniform(0.4, 2.0))

            # â€” ØªÙ…Ø±ÙŠØ± Ù„Ø£Ø¹Ù„Ù‰ â€”
            elif action == 'scroll_up':
                await _human_scroll(page, -random.randint(50, 220))
                await asyncio.sleep(random.uniform(0.3, 1.2))

            # â€” Ø¶ØºØ·Ø© Ø¹Ù„Ù‰ Ø¹Ù†ØµØ± Ù…Ø±Ø¦ÙŠ â€”
            elif action == 'click':
                try:
                    vis = await _get_clickables(page)
                    if vis:
                        el, b = random.choice(vis[:20])
                        x = b['x'] + b['width']  / 2 + random.uniform(-5, 5)
                        y = b['y'] + b['height'] / 2 + random.uniform(-4, 4)
                        await _human_move(page, x, y)
                        await asyncio.sleep(random.uniform(0.12, 0.45))
                        await page.mouse.down()
                        await asyncio.sleep(random.uniform(0.05, 0.18))   # Ù…Ø¯Ø© Ø§Ù„Ø¶ØºØ·
                        await page.mouse.up()
                        await asyncio.sleep(random.uniform(0.6, 2.5))
                except Exception:
                    pass

            # â€” Ø­Ø±ÙƒØ© Ù…ÙˆØ³ Ø¹Ø´ÙˆØ§Ø¦ÙŠØ© (ØªØµÙØ­ Ø¨Ø¯ÙˆÙ† Ø¶ØºØ·) â€”
            elif action == 'move':
                pts = random.randint(1, 3)
                for _ in range(pts):
                    await _human_move(page,
                                      random.uniform(10, vw - 10),
                                      random.uniform(10, vh - 10))
                    await asyncio.sleep(random.uniform(0.15, 0.6))

            # â€” ØªÙˆÙ‚Ù Ù‚Ø±Ø§Ø¡Ø© â€”
            elif action == 'pause':
                await asyncio.sleep(random.uniform(1.2, 4.0))

            # â€” hover Ø¹Ù„Ù‰ Ø¹Ù†ØµØ± â€”
            elif action == 'hover':
                try:
                    vis = await _get_clickables(page)
                    if vis:
                        el, b = random.choice(vis[:15])
                        await _human_move(page,
                                          b['x'] + b['width']  / 2,
                                          b['y'] + b['height'] / 2)
                        await asyncio.sleep(random.uniform(0.4, 1.6))
                except Exception:
                    pass

        total_s = int(time.time() - t_start)
        with _lock:
            _stats['ok'] += 1
        sc = f'[{resp.status}]' if resp else ''
        add_log(f'âœ“ {sid:04d} {sc} {dev["name"]}  {nav_ms}ms  {total_s}s  {locale}')

    except Exception as e:
        with _lock:
            _stats['err'] += 1
        add_log(f'âœ— {sid:04d} {type(e).__name__}: {str(e)[:80]}')
    finally:
        with _lock:
            _stats['active'] -= 1
        if browser:
            try: await browser.close()
            except: pass

# ===== Master runner =====
async def _master(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix=True):
    _state['stop'] = False
    reset_stats(count)

    async with async_playwright() as pw:
        sem = asyncio.Semaphore(concurrency)

        async def bounded(i):
            if _state['stop']:
                return
            if err_thresh > 0:
                with _lock:
                    done = _stats['ok'] + _stats['err']
                    if done >= 10 and _stats['err'] / done * 100 >= err_thresh:
                        _state['stop'] = True
                        return
            async with sem:
                if _state['stop']:
                    return
                await run_session(pw, url, proxy or None, duration, i, jitter, traffic_mix)

        await asyncio.gather(*[bounded(i) for i in range(1, count+1)])

    _state['running'] = False

def _thread(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix=True):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_master(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix))
    loop.close()

# Background RPS history tracker
def _rps_tracker():
    prev_done = 0
    while True:
        time.sleep(1)
        with _lock:
            if _stats['t0']:
                elapsed = time.time() - _stats['t0']
                done    = _stats['ok'] + _stats['err']
                rps     = done / max(elapsed, 1)
                _stats['rps_hist'].append(round(rps, 2))
                if len(_stats['rps_hist']) > 60:
                    _stats['rps_hist'].pop(0)

threading.Thread(target=_rps_tracker, daemon=True).start()

# ===== Routes =====
@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

@app.route('/test_proxy', methods=['POST'])
def test_proxy():
    import urllib.request
    raw = (request.json or {}).get('proxy', '').strip()
    cfg = parse_proxy(raw)
    t0  = time.time()

    try:
        req = urllib.request.Request('https://ipinfo.io/json', headers={'User-Agent':'curl/7.88'})
        with urllib.request.urlopen(req, timeout=8) as r:
            base = json.loads(r.read())
    except Exception as e:
        base = {'ip':'?','country':'?','org':str(e)[:60]}

    if not cfg:
        return jsonify({'ok':True,'mode':'direct','ip':base.get('ip'),
                        'country':base.get('country'),'org':base.get('org',''),'ms':0,
                        'msg':'Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø¨Ø±ÙˆÙƒØ³ÙŠ â€” IP Ø§Ù„Ø³ÙŠØ±ÙØ± Ø§Ù„Ù…Ø¨Ø§Ø´Ø±'})

    server   = cfg['server']
    username = cfg.get('username','')
    password = cfg.get('password','')
    try:
        proxy_url = server
        if username:
            scheme, rest = server.split('://', 1)
            proxy_url = f'{scheme}://{urllib.request.quote(username,safe="")}:{urllib.request.quote(password,safe="")}@{rest}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy_url,'https':proxy_url}))
        req2   = urllib.request.Request('https://ipinfo.io/json', headers={'User-Agent':'curl/7.88'})
        with opener.open(req2, timeout=12) as r:
            data = json.loads(r.read())
        ms      = round((time.time()-t0)*1000)
        same_ip = data.get('ip') == base.get('ip')
        return jsonify({'ok':True,'mode':'proxy','ip':data.get('ip'),'country':data.get('country'),
                        'org':data.get('org',''),'city':data.get('city',''),'ms':ms,'same_ip':same_ip,
                        'msg':'âš ï¸ Ù†ÙØ³ IP Ø§Ù„Ø³ÙŠØ±ÙØ±! Ø§Ù„Ø¨Ø±ÙˆÙƒØ³ÙŠ Ù„Ø§ ÙŠØ¹Ù…Ù„' if same_ip else 'âœ“ Ø§Ù„Ø¨Ø±ÙˆÙƒØ³ÙŠ ÙŠØ¹Ù…Ù„'})
    except Exception as e:
        return jsonify({'ok':False,'msg':f'ÙØ´Ù„: {type(e).__name__}: {str(e)[:120]}',
                        'ms':round((time.time()-t0)*1000)})

@app.route('/start', methods=['POST'])
def start():
    if _state['running']:
        return jsonify({'error':'already_running'}), 400
    d   = request.json or {}
    url = d.get('url','').strip()
    if not url:
        return jsonify({'error':'url_required'}), 400
    _state['stop']    = False
    _state['running'] = True
    threading.Thread(target=_thread, daemon=True, args=(
        url,
        d.get('proxy','').strip() or None,
        int(d.get('count',50)),
        int(d.get('concurrency',3)),
        float(d.get('duration',15)),
        float(d.get('jitter',0)),
        float(d.get('err_thresh',0)),
        bool(d.get('traffic_mix', True)),
    )).start()
    return jsonify({'ok':True})

@app.route('/stop', methods=['POST'])
def stop():
    _state['stop']    = True
    _state['running'] = False   # ÙÙˆØ±ÙŠ â€” ÙŠØ®Ù„Ù‘ÙŠ Ø§Ù„Ù€ UI ÙŠØ³ØªØ¬ÙŠØ¨ ÙÙˆØ±Ø§Ù‹
    return jsonify({'ok':True})

@app.route('/export')
def export():
    with _lock:
        log = list(_stats['log'])
    return Response('\n'.join(log), mimetype='text/plain',
                    headers={'Content-Disposition':'attachment; filename=kw_tester_log.txt'})

@app.route('/snap')
def snap():
    with _lock:
        elapsed = time.time() - _stats['t0'] if _stats['t0'] else 0
        done    = _stats['ok'] + _stats['err']
        rps     = done / max(elapsed, 1)
        total   = _stats['total']
        avg_ms  = int(sum(_stats['times'])/len(_stats['times'])) if _stats['times'] else 0
        eta     = int((total-done)/max(rps,0.01)) if rps>0 and done<total and _state['running'] else 0
        return jsonify({
            'running': _state['running'],
            'ok': _stats['ok'], 'err': _stats['err'],
            'active': _stats['active'], 'total': total,
            'elapsed': round(elapsed,1), 'rps': round(rps,2),
            'avg_ms': avg_ms, 'eta': eta,
            'ok_rate': round(_stats['ok']/max(done,1)*100, 1),
            'codes': dict(_stats['codes']),
        })

@app.route('/stats')
def stats_sse():
    def stream():
        while True:
            with _lock:
                elapsed = time.time() - _stats['t0'] if _stats['t0'] else 0
                done    = _stats['ok'] + _stats['err']
                rps     = done / max(elapsed, 1)
                total   = _stats['total']
                avg_ms  = int(sum(_stats['times'])/len(_stats['times'])) if _stats['times'] else 0
                eta     = int((total-done)/max(rps,0.01)) if rps>0 and done<total and _state['running'] else 0
                ok_rate = round(_stats['ok']/max(done,1)*100, 1)
                payload = {
                    'running': _state['running'],
                    'ok': _stats['ok'], 'err': _stats['err'],
                    'active': _stats['active'], 'total': total,
                    'elapsed': round(elapsed,1), 'rps': round(rps,2),
                    'avg_ms': avg_ms, 'eta': eta, 'ok_rate': ok_rate,
                    'codes': dict(_stats['codes']),
                    'devices': dict(_stats['devices']),
                    'rps_hist': list(_stats['rps_hist']),
                    'log': list(_stats['log'][-20:]),
                }
            yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
            time.sleep(1)
    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

# ===== HTML =====
HTML_PAGE = r'''<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KW Mobile Tester</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--border2:#21262d;--txt:#e6edf3;--muted:#8b949e;--ok:#3fb950;--err:#f85149;--act:#58a6ff;--spd:#d2a8ff;--warn:#e3b341}
body{background:var(--bg);color:var(--txt);font-family:'Segoe UI',Tahoma,Arial,sans-serif;min-height:100vh;padding:20px 16px 40px;max-width:860px;margin:0 auto}
h1{color:var(--act);font-size:18px;margin-bottom:20px;display:flex;align-items:center;gap:8px;letter-spacing:-.3px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:14px}
.ct{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:4px;margin-top:10px}
label:first-of-type{margin-top:0}
input,select{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--txt);padding:9px 12px;font-size:14px;outline:none;transition:border .15s}
input:focus{border-color:var(--act);box-shadow:0 0 0 3px #58a6ff18}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-top:10px}
.btn{padding:11px 0;border-radius:8px;border:none;cursor:pointer;font-size:14px;font-weight:700;width:100%;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.98)}
.btn-go{background:linear-gradient(135deg,#238636,#2ea043);color:#fff}
.btn-stop{background:linear-gradient(135deg,#b91c1c,#da3633);color:#fff}
.btn:disabled{opacity:.3;cursor:not-allowed;transform:none}
.btn-sm{padding:0 14px;height:38px;border-radius:7px;border:1px solid var(--border);background:var(--border2);color:var(--txt);cursor:pointer;font-size:12px;font-weight:600;white-space:nowrap;transition:background .15s}
.btn-sm:hover{background:var(--border)}
.btn-sm:disabled{opacity:.4;cursor:not-allowed}
/* stats */
.stats5{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.stat{background:var(--bg);border:1px solid var(--border2);border-radius:10px;padding:14px 8px;text-align:center}
.stat .num{font-size:26px;font-weight:800;line-height:1}
.stat .lbl{font-size:10px;color:var(--muted);margin-top:4px}
.c-ok{color:var(--ok)}.c-err{color:var(--err)}.c-act{color:var(--act)}.c-spd{color:var(--spd)}.c-ms{color:var(--warn)}
/* progress */
.prog-wrap{margin-top:14px}
.prog-bar{height:8px;background:var(--border2);border-radius:4px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,#238636,#3fb950);border-radius:4px;transition:width .7s ease;width:0%}
.prog-info{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:5px}
/* badge */
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;border-radius:20px;font-size:11px;font-weight:700}
.b-run{background:#1f6feb22;color:var(--act);border:1px solid #1f6feb55}
.b-idle{background:var(--border2);color:var(--muted);border:1px solid var(--border)}
.b-done{background:#0f2d1a;color:var(--ok);border:1px solid #238636}
/* proxy result */
.px-ok{background:#0f2d1a;border:1px solid #238636;color:var(--ok);padding:9px 13px;border-radius:7px;font-size:12px;font-family:monospace;direction:ltr;margin-top:7px}
.px-warn{background:#2d1f0f;border:1px solid #9e6a03;color:var(--warn);padding:9px 13px;border-radius:7px;font-size:12px;font-family:monospace;direction:ltr;margin-top:7px}
.px-err{background:#2d0f0f;border:1px solid var(--err);color:var(--err);padding:9px 13px;border-radius:7px;font-size:12px;font-family:monospace;direction:ltr;margin-top:7px}
/* sparkline */
.spark-wrap{background:var(--bg);border:1px solid var(--border2);border-radius:8px;padding:10px 12px;margin-top:12px}
.spark-label{font-size:10px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between}
svg.sparkline{width:100%;height:50px;display:block;overflow:visible}
/* codes bar */
.codes-bar{height:22px;border-radius:5px;overflow:hidden;display:flex;margin-top:8px;background:var(--border2)}
.code-seg{height:100%;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;font-weight:700;transition:width .5s;overflow:hidden;white-space:nowrap}
.codes-legend{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap}
.leg-item{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)}
.leg-dot{width:8px;height:8px;border-radius:2px}
/* devices */
.dev-chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:2px}
.dev-chip{background:var(--border2);border:1px solid var(--border);border-radius:5px;padding:3px 9px;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:4px}
.dev-chip.active{border-color:var(--act);color:var(--act)}
.dev-cnt{background:var(--act);color:#000;border-radius:3px;padding:0 4px;font-size:10px;font-weight:700}
/* log */
.log-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.log-filters{display:flex;gap:5px}
.log-btn{padding:2px 9px;border-radius:5px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:11px;cursor:pointer;transition:all .15s}
.log-btn.active{background:var(--act);border-color:var(--act);color:#000;font-weight:700}
.log-box{background:#010409;border:1px solid var(--border2);border-radius:8px;padding:10px 12px;height:220px;overflow-y:auto;font-family:'Courier New',monospace;font-size:11.5px;direction:ltr}
.log-box::-webkit-scrollbar{width:3px}
.log-box::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.log-ok{color:var(--ok)}.log-err{color:var(--err)}
/* toast */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 22px;border-radius:8px;font-size:13px;font-weight:600;opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
.toast-ok{background:#0f2d1a;border:1px solid var(--ok);color:var(--ok)}
.toast-err{background:#2d0f0f;border:1px solid var(--err);color:var(--err)}
.toast-info{background:var(--card);border:1px solid var(--border);color:var(--txt)}
/* rate indicator */
.rate-pill{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:700;margin-right:8px}
.rate-hi{background:#0f2d1a;color:var(--ok)}.rate-mid{background:#2d1f0f;color:var(--warn)}.rate-lo{background:#2d0f0f;color:var(--err)}
/* hdr util */
.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
</style>
</head>
<body>

<h1>
  <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M17 2H7c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H7V6h10v10z"/></svg>
  KW Mobile Tester
</h1>

<!-- Settings -->
<div class="card">
  <div class="ct">âš™ï¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§Ù„Ø§Ø®ØªØ¨Ø§Ø±</div>

  <label>Ø±Ø§Ø¨Ø· Ø§Ù„Ù…ÙˆÙ‚Ø¹ *</label>
  <input id="url" type="url" placeholder="https://example.com" oninput="saveCfg()">

  <label>Ø§Ù„Ø¨Ø±ÙˆÙƒØ³ÙŠ (Ø§Ø®ØªÙŠØ§Ø±ÙŠ)</label>
  <div style="display:flex;gap:7px;align-items:center">
    <input id="proxy" type="text" placeholder="user:pass@host:port  Ø£Ùˆ  http://..." style="flex:1" oninput="saveCfg()">
    <button class="btn-sm" id="btnTest" onclick="testProxy()">ðŸ” Ø§Ø®ØªØ¨Ø§Ø±</button>
  </div>
  <div id="proxyResult" style="display:none"></div>

  <div class="g4">
    <div><label>Ø¹Ø¯Ø¯ Ø§Ù„Ø²ÙŠØ§Ø±Ø§Øª</label><input id="count" type="number" value="30" min="1" max="5000" oninput="saveCfg()"></div>
    <div><label>ØªØ²Ø§Ù…Ù†</label><input id="conc" type="number" value="3" min="1" max="15" oninput="saveCfg()"></div>
    <div><label>Ù…Ø¯Ø© Ø§Ù„Ø¬Ù„Ø³Ø© (Ø«)</label><input id="dur" type="number" value="20" min="5" max="60" oninput="saveCfg()"></div>
    <div><label>Ø¬ÙŠØªØ± (Ø«) â“˜</label><input id="jitter" type="number" value="0" min="0" max="30" step="0.5" title="ØªØ£Ø®ÙŠØ± Ø¹Ø´ÙˆØ§Ø¦ÙŠ Ø¨ÙŠÙ† Ø¨Ø¯Ø¡ Ø§Ù„Ø¬Ù„Ø³Ø§Øª" oninput="saveCfg()"></div>
  </div>

  <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
    <label style="margin:0;white-space:nowrap">Ø¥ÙŠÙ‚Ø§Ù ØªÙ„Ù‚Ø§Ø¦ÙŠ Ø¥Ø°Ø§ ÙˆØµÙ„Øª Ø§Ù„Ø£Ø®Ø·Ø§Ø¡</label>
    <input id="errThresh" type="number" value="0" min="0" max="100" style="width:70px" title="0 = Ù…Ø¹Ø·Ù‘Ù„" oninput="saveCfg()">
    <span style="font-size:12px;color:var(--muted)">% (0 = Ù…Ø¹Ø·Ù‘Ù„)</span>
  </div>

  <div class="g2" style="margin-top:12px">
    <button class="btn btn-go"   id="btnGo"   onclick="doStart()">â–¶ Ø§Ø¨Ø¯Ø£ Ø§Ù„Ø§Ø®ØªØ¨Ø§Ø±</button>
    <button class="btn btn-stop" id="btnStop" onclick="doStop()" disabled>â¹ Ø¥ÙŠÙ‚Ø§Ù</button>
  </div>
</div>

<!-- Stats -->
<div class="card">
  <div class="hdr">
    <span class="ct" style="margin:0">ðŸ“Š Ø¥Ø­ØµØ§Ø¦ÙŠØ§Øª Ù…Ø¨Ø§Ø´Ø±Ø©</span>
    <div style="display:flex;align-items:center;gap:8px">
      <span id="okRate" class="rate-pill rate-hi" style="display:none"></span>
      <span class="badge b-idle" id="badge">â¹ Ù…ØªÙˆÙ‚Ù</span>
    </div>
  </div>

  <div class="stats5">
    <div class="stat"><div class="num c-ok"  id="sOk">0</div><div class="lbl">âœ“ Ù†Ø¬Ø§Ø­</div></div>
    <div class="stat"><div class="num c-err" id="sErr">0</div><div class="lbl">âœ— Ø£Ø®Ø·Ø§Ø¡</div></div>
    <div class="stat"><div class="num c-act" id="sAct">0</div><div class="lbl">ðŸ”µ Ù†Ø´Ø·</div></div>
    <div class="stat"><div class="num c-spd" id="sRps">0.0</div><div class="lbl">âš¡ Ø¬Ù„Ø³Ø©/Ø«</div></div>
    <div class="stat"><div class="num c-ms"  id="sMs">â€”</div><div class="lbl">â± avg ms</div></div>
  </div>

  <div class="prog-wrap">
    <div class="prog-bar"><div class="prog-fill" id="pFill"></div></div>
    <div class="prog-info">
      <span id="pDone">0 / 0</span>
      <span id="pEta"></span>
      <span id="pTime">0s</span>
    </div>
  </div>

  <!-- Sparkline -->
  <div class="spark-wrap">
    <div class="spark-label">
      <span>Ø¬Ù„Ø³Ø©/Ø«Ø§Ù†ÙŠØ© â€” Ø¢Ø®Ø± 60s</span>
      <span id="sparkMax" style="color:var(--spd)"></span>
    </div>
    <svg class="sparkline" id="sparkSvg" preserveAspectRatio="none"></svg>
  </div>
</div>

<!-- Status codes -->
<div class="card">
  <div class="ct">ðŸ”¢ ØªÙˆØ²ÙŠØ¹ Ø§Ù„Ø§Ø³ØªØ¬Ø§Ø¨Ø§Øª</div>
  <div class="codes-bar" id="codesBar"><span style="color:var(--muted);font-size:11px;padding:3px 8px">Ù„Ø§ Ø¨ÙŠØ§Ù†Ø§Øª Ø¨Ø¹Ø¯</span></div>
  <div class="codes-legend">
    <div class="leg-item"><div class="leg-dot" style="background:#3fb950"></div>2xx Ù†Ø¬Ø§Ø­</div>
    <div class="leg-item"><div class="leg-dot" style="background:#58a6ff"></div>3xx ØªØ­ÙˆÙŠÙ„</div>
    <div class="leg-item"><div class="leg-dot" style="background:#e3b341"></div>4xx Ø®Ø·Ø£ Ø¹Ù…ÙŠÙ„</div>
    <div class="leg-item"><div class="leg-dot" style="background:#f85149"></div>5xx Ø®Ø·Ø£ Ø³ÙŠØ±ÙØ±</div>
  </div>
  <div id="codesDetail" style="margin-top:8px;font-size:11px;color:var(--muted);direction:ltr"></div>
</div>

<!-- Devices -->
<div class="card">
  <div class="ct">ðŸ“± Ø§Ù„Ø£Ø¬Ù‡Ø²Ø© Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù…Ø©</div>
  <div class="dev-chips" id="devChips"></div>
</div>

<!-- Log -->
<div class="card">
  <div class="log-hdr">
    <span class="ct" style="margin:0">ðŸ“‹ Ø³Ø¬Ù„ Ø§Ù„Ø¹Ù…Ù„ÙŠØ§Øª</span>
    <div style="display:flex;gap:5px;align-items:center">
      <div class="log-filters">
        <button class="log-btn active" id="fAll"  onclick="setFilter('all')">Ø§Ù„ÙƒÙ„</button>
        <button class="log-btn"        id="fOk"   onclick="setFilter('ok')">âœ“</button>
        <button class="log-btn"        id="fErr"  onclick="setFilter('err')">âœ—</button>
      </div>
      <a href="/export" class="btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;padding:2px 9px;height:25px">â¬‡ ØªØµØ¯ÙŠØ±</a>
    </div>
  </div>
  <div class="log-box" id="logBox"><span style="color:#484f58">Ø¬Ø§Ù‡Ø² Ù„Ù„Ø¨Ø¯Ø¡...</span></div>
</div>

<script>
// ===== config persist =====
function saveCfg(){
  try{localStorage.setItem('kwt2',JSON.stringify({
    url:$('url').value, proxy:$('proxy').value, count:$('count').value,
    conc:$('conc').value, dur:$('dur').value, jitter:$('jitter').value,
    errThresh:$('errThresh').value
  }));}catch(e){}
}
function loadCfg(){
  try{
    const c=JSON.parse(localStorage.getItem('kwt2')||'{}');
    if(c.url)    $('url').value=c.url;
    if(c.proxy)  $('proxy').value=c.proxy;
    if(c.count)  $('count').value=c.count;
    if(c.conc)   $('conc').value=c.conc;
    if(c.dur)    $('dur').value=c.dur;
    if(c.jitter) $('jitter').value=c.jitter;
    if(c.errThresh) $('errThresh').value=c.errThresh;
  }catch(e){}
}

// ===== utils =====
function $(id){return document.getElementById(id)}
function fmt(s){return s>=3600?`${Math.floor(s/3600)}h${Math.floor(s%3600/60)}m`:s>=60?`${Math.floor(s/60)}m${s%60}s`:`${s}s`}

// ===== toast =====
function toast(msg,type='info'){
  const t=document.createElement('div');
  t.className=`toast toast-${type}`;t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(()=>t.style.opacity='1',10);
  setTimeout(()=>{t.style.opacity='0';setTimeout(()=>t.remove(),400);},3500);
}

// ===== proxy test =====
function testProxy(){
  const prx=$('proxy').value.trim();
  const btn=$('btnTest'), box=$('proxyResult');
  btn.disabled=true; btn.textContent='â³';
  box.style.display='none';
  fetch('/test_proxy',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({proxy:prx})})
  .then(r=>r.json()).then(d=>{
    btn.disabled=false; btn.textContent='ðŸ” Ø§Ø®ØªØ¨Ø§Ø±';
    box.style.display='block';
    if(!d.ok){box.className='px-err';box.textContent=d.msg;return;}
    if(d.mode==='direct'){
      box.className='px-warn';
      box.textContent=`âš  Ø¨Ø¯ÙˆÙ† Ø¨Ø±ÙˆÙƒØ³ÙŠ â€” IP: ${d.ip}  ${d.country}  ${d.org}`;
    } else if(d.same_ip){
      box.className='px-warn';
      box.textContent=`âš  Ù†ÙØ³ IP Ø§Ù„Ø³ÙŠØ±ÙØ±!  IP: ${d.ip}  ${d.ms}ms`;
    } else {
      box.className='px-ok';
      box.textContent=`âœ“ ÙŠØ¹Ù…Ù„  IP: ${d.ip}  ${d.country} ${d.city}  ${d.org}  ${d.ms}ms`;
    }
  }).catch(e=>{
    btn.disabled=false; btn.textContent='ðŸ” Ø§Ø®ØªØ¨Ø§Ø±';
    box.className='px-err'; box.style.display='block'; box.textContent='Ø®Ø·Ø£: '+e;
  });
}

// ===== start / stop =====
function doStart(){
  const url=$('url').value.trim();
  if(!url){toast('Ø£Ø¯Ø®Ù„ Ø±Ø§Ø¨Ø· Ø§Ù„Ù…ÙˆÙ‚Ø¹','err');return;}
  saveCfg();
  fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      url, proxy:$('proxy').value.trim(),
      count:+$('count').value||30,
      concurrency:+$('conc').value||3,
      duration:+$('dur').value||15,
      jitter:+$('jitter').value||0,
      err_thresh:+$('errThresh').value||0,
    })})
  .then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'err');return;}
    $('btnGo').disabled=true; $('btnStop').disabled=false;
    $('logBox').innerHTML='';
    subscribe();
    toast('Ø¨Ø¯Ø£ Ø§Ù„Ø§Ø®ØªØ¨Ø§Ø±','info');
  });
}
function doStop(){
  fetch('/stop',{method:'POST'});
  $('btnStop').disabled=true;
  toast('Ø¬Ø§Ø±ÙŠ Ø§Ù„Ø¥ÙŠÙ‚Ø§Ù...','info');
}

// ===== log filter =====
let _logFilter='all', _lastLog=[];
function setFilter(f){
  _logFilter=f;
  ['all','ok','err'].forEach(x=>$('f'+x[0].toUpperCase()+x.slice(1)).className='log-btn'+(x===f?' active':''));
  renderLog(_lastLog);
}
function renderLog(log){
  _lastLog=log;
  const filtered=log.filter(l=>_logFilter==='all'||(l.startsWith('âœ“')&&_logFilter==='ok')||(l.startsWith('âœ—')&&_logFilter==='err'));
  $('logBox').innerHTML=[...filtered].reverse().map(l=>`<div class="${l.startsWith('âœ“')?'log-ok':'log-err'}">${l}</div>`).join('') || '<span style="color:#484f58">Ù„Ø§ ØªÙˆØ¬Ø¯ Ù†ØªØ§Ø¦Ø¬</span>';
}

// ===== sparkline =====
function drawSparkline(data){
  const svg=$('sparkSvg');
  const W=svg.clientWidth||600, H=50;
  if(!data||!data.length){svg.innerHTML='';return;}
  const max=Math.max(...data,0.01);
  $('sparkMax').textContent=`peak: ${max.toFixed(1)}/s`;
  const n=data.length;
  const pts=data.map((v,i)=>{
    const x=(i/(Math.max(n-1,1)))*W;
    const y=H-(v/max)*H*0.88+2;
    return `${i===0?'M':'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const areaClose=`L${W},${H} L0,${H} Z`;
  svg.innerHTML=`<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#58a6ff" stop-opacity="0.25"/>
    <stop offset="100%" stop-color="#58a6ff" stop-opacity="0"/>
  </linearGradient></defs>
  <path d="${pts} ${areaClose}" fill="url(#sg)"/>
  <path d="${pts}" fill="none" stroke="#58a6ff" stroke-width="1.5" stroke-linejoin="round"/>`;
}

// ===== codes bar =====
const CODE_COLORS={'2xx':'#3fb950','3xx':'#58a6ff','4xx':'#e3b341','5xx':'#f85149','?':'#484f58'};
function updateCodes(codes){
  const total=Object.values(codes).reduce((a,b)=>a+b,0)||1;
  const groups={'2xx':0,'3xx':0,'4xx':0,'5xx':0,'?':0};
  Object.entries(codes).forEach(([code,cnt])=>{
    const c=parseInt(code);
    if(c>=200&&c<300)groups['2xx']+=cnt;
    else if(c>=300&&c<400)groups['3xx']+=cnt;
    else if(c>=400&&c<500)groups['4xx']+=cnt;
    else if(c>=500)groups['5xx']+=cnt;
    else groups['?']+=cnt;
  });
  const bar=$('codesBar');
  const segs=Object.entries(groups).filter(([,v])=>v>0).map(([k,v])=>{
    const pct=(v/total*100).toFixed(1);
    return `<div class="code-seg" style="width:${pct}%;background:${CODE_COLORS[k]}" title="${k}: ${v} (${pct}%)">${parseFloat(pct)>8?k:'&nbsp;'}</div>`;
  }).join('');
  bar.innerHTML=segs||'<span style="color:var(--muted);font-size:11px;padding:3px 8px">Ù„Ø§ Ø¨ÙŠØ§Ù†Ø§Øª</span>';
  $('codesDetail').textContent=Object.entries(codes).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${k}: ${v}`).join('  ');
}

// ===== devices =====
function updateDevices(devs){
  const box=$('devChips');
  if(!Object.keys(devs).length){
    box.innerHTML='<span style="color:var(--muted);font-size:12px">Ù„Ø§ Ø¨ÙŠØ§Ù†Ø§Øª Ø¨Ø¹Ø¯</span>';
    return;
  }
  box.innerHTML=Object.entries(devs).sort((a,b)=>b[1]-a[1]).map(([name,cnt])=>
    `<div class="dev-chip active"><span>${name}</span><span class="dev-cnt">${cnt}</span></div>`
  ).join('');
}

// ===== SSE =====
let es=null, prevRunning=false;
function subscribe(){
  if(es)es.close();
  es=new EventSource('/stats');
  es.onmessage=e=>{
    const d=JSON.parse(e.data);

    $('sOk').textContent=d.ok;
    $('sErr').textContent=d.err;
    $('sAct').textContent=d.active;
    $('sRps').textContent=d.rps.toFixed(1);
    $('sMs').textContent=d.avg_ms?d.avg_ms+'ms':'â€”';

    const done=d.ok+d.err;
    const pct=d.total?Math.round(done/d.total*100):0;
    $('pFill').style.width=pct+'%';
    $('pDone').textContent=`${done} / ${d.total}  (${pct}%)`;
    $('pTime').textContent=d.elapsed+'s';
    $('pEta').textContent=d.eta>0?`ETA: ${fmt(d.eta)}`:'';

    // ok rate pill
    const rp=$('okRate');
    if(done>0){
      rp.style.display='inline-block';
      rp.textContent=d.ok_rate+'%';
      rp.className='rate-pill '+(d.ok_rate>=80?'rate-hi':d.ok_rate>=50?'rate-mid':'rate-lo');
    } else rp.style.display='none';

    const badge=$('badge');
    if(d.running){
      badge.className='badge b-run'; badge.textContent='ðŸŸ¢ ÙŠØ¹Ù…Ù„';
    } else if(done>0 && done===d.total){
      badge.className='badge b-done'; badge.textContent='âœ“ Ø§ÙƒØªÙ…Ù„';
    } else {
      badge.className='badge b-idle'; badge.textContent='â¹ Ù…ØªÙˆÙ‚Ù';
    }

    if(!d.running && prevRunning){
      $('btnGo').disabled=false; $('btnStop').disabled=true;
      toast(`Ø§Ù†ØªÙ‡Ù‰! âœ“${d.ok} âœ—${d.err}`, d.err===0?'ok':'info');
    }
    prevRunning=d.running;

    drawSparkline(d.rps_hist);
    updateCodes(d.codes);
    updateDevices(d.devices);
    if(d.log&&d.log.length) renderLog(d.log);
  };
}

// ===== init =====
loadCfg();
subscribe();
</script>
</body>
</html>
'''

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5555
    print(f"KW Mobile Tester v2 â†’ http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, threaded=True)
