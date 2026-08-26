#!/usr/bin/env python3
# /opt/kw_tester_app.py  —  KW Mobile Tester v2

from flask import Flask, render_template_string, request, jsonify, Response
from playwright.async_api import async_playwright
import asyncio, threading, json, time, random, string, os

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
    'log': [], 'codes': {}, 'times': [], 'devices': {}, 'rps_hist': [], 'dead': 0,
}
_lock = threading.Lock()

# === زوّار عائدون (#2) ===
# بعد كل جلسة ناجحة نلتقط storage_state (الكوكيز اللي الموقع نفسه حطها زي _ga/_fbp)،
# وجزء من الجلسات الجديدة يبدأ من واحدة محفوظة = الموقع يشوفه "returning visitor".
RETURNING_RATE = 0.28          # نسبة الجلسات اللي تبان زائر عائد
_ctx_states     = []           # بول snapshots للكوكيز (bounded)
_CTX_STATES_MAX = 200
def _save_ctx_state(st):
    if not st:
        return
    with _lock:
        _ctx_states.append(st)
        if len(_ctx_states) > _CTX_STATES_MAX:
            _ctx_states.pop(0)
def _pick_ctx_state():
    with _lock:
        return random.choice(_ctx_states) if _ctx_states else None

# === شخصيات الجلسات (#4) ===
# توزيع طبيعي: زائر يخرج بسرعة، ماسح عادي، قارئ متعمّق.
PERSONAS = (
    [('bouncer', 0.30, 0.55)] * 20 +   # مدة قصيرة، يخرج بسرعة (bounce)
    [('scanner', 0.85, 1.15)] * 50 +   # تصفّح عادي
    [('reader',  1.25, 1.70)] * 30     # قراءة متعمّقة، وقت أطول
)

# === تشكيل حسب ساعة اليوم (#3) ===
# منحنى شبيه بترافيك فيسبوك: ذروة مساءً، هدوء الفجر (0.0..1.0 لكل ساعة محلية).
_HOUR_WEIGHTS = [
    0.20, 0.15, 0.12, 0.10, 0.12, 0.18,   # 0-5  فجر
    0.30, 0.45, 0.60, 0.70, 0.72, 0.75,   # 6-11 صباح
    0.80, 0.78, 0.72, 0.70, 0.75, 0.85,   # 12-17 ظهر/عصر
    0.95, 1.00, 0.98, 0.85, 0.60, 0.35,   # 18-23 ذروة المساء
]
def _hour_weight():
    try:
        return _HOUR_WEIGHTS[time.localtime().tm_hour]
    except Exception:
        return 0.8

# دومينات تعني إن الـ Smartlink ميت/موقوف (صفحة Adsterra الاحتياطية) —
# الهبوط عليها = زيارة مش هتتحسب، فنوقف بدل ما نحرق بروكسي على الفاضي
DEAD_HOSTS = ('adzilla.meme', 'sedoparking.com', 'parkingcrew.net', 'bodis.com',
              'above.com', 'dnsrsearch.com', 'voodoo.com', 'uniregistry.com',
              'hugedomains.com', 'dan.com', 'afternic.com', 'sedo.com')
DEAD_LIMIT = 5   # عدد الهبوط الميت المتتالي قبل الإيقاف التلقائي

# علامات صفحة parked/معروضة للبيع — كشف بالمحتوى مش بالنطاق بس
PARKED_MARKERS = ('this domain is for sale', 'buy this domain', 'the domain has expired',
                  'domain is parked', 'this webpage is parked', 'parked free of charge',
                  'domain for sale', 'الدومين معروض للبيع')

class DeadLink(Exception):
    """الرابط هبط على صفحة احتياطية ميتة بدل عرض حقيقي."""
    pass

class NoProxy(Exception):
    """حماية تسريب الآي بي: مفيش بروكسي صالح للجلسة — تُلغى قبل فتح المتصفح."""
    pass

def _probe_proxy(pstr, timeout=12):
    """يتأكد إن البروكسي حي فعليًا (مش معلّق) عبر طلب HTTP سريع. True=حي."""
    cfg = parse_proxy(pstr)
    if not cfg:
        return False
    try:
        import urllib.request
        server = cfg['server']
        user   = cfg.get('username', '')
        pw     = cfg.get('password', '')
        purl   = server.replace('://', '://%s:%s@' % (user, pw)) if user else server
        h = urllib.request.HTTPHandler()
        proxy_h = urllib.request.ProxyHandler({'http': purl, 'https': purl})
        opener = urllib.request.build_opener(proxy_h)
        req = urllib.request.Request('https://ipinfo.io/ip',
                                     headers={'User-Agent': 'curl/8'})
        with opener.open(req, timeout=timeout) as r:
            return r.status == 200 and bool(r.read(4))
    except Exception:
        return False

def _host_of(u):
    try:
        from urllib.parse import urlparse
        return (urlparse(u).hostname or '').lower()
    except Exception:
        return ''

def _is_dead_host(host):
    return any(host == d or host.endswith('.' + d) for d in DEAD_HOSTS)

async def _looks_parked(page):
    """كشف صفحة معروضة للبيع/parked من عنوان الصفحة أو أول جزء من نصّها."""
    try:
        title = (await page.title() or '').lower()
        if any(m in title for m in PARKED_MARKERS):
            return True
        txt = (await page.evaluate("document.body ? document.body.innerText.slice(0,1500) : ''") or '').lower()
        return any(m in txt for m in PARKED_MARKERS)
    except Exception:
        return False

def parse_proxy(raw):
    if not raw:
        return None
    import uuid
    raw = raw.strip().replace('RANDOM', uuid.uuid4().hex[:10])
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
                       'log':[],'codes':{},'times':[],'devices':{},'rps_hist':[],'dead':0})

def add_log(msg):
    with _lock:
        _stats['log'].append(msg)
        if len(_stats['log']) > 300:
            _stats['log'].pop(0)

# ===== استمرارية الحملة (استئناف تلقائي بعد أي rerun للرنر) =====
# نحفظ آخر حملة جنب السكربت. على الرنر ده /mnt/work/kw_campaign.json
# اللي بيتزامن مع ghstate ويترجّع كل جوب — فالبوت يكمّل لوحده.
_HERE = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_FILE = os.environ.get('KW_CAMPAIGN_FILE',
                               os.path.join(_HERE, 'kw_campaign.json'))

def save_campaign(cfg):
    try:
        with open(CAMPAIGN_FILE, 'w') as f:
            json.dump(cfg, f)
    except Exception:
        pass

def load_campaign():
    try:
        with open(CAMPAIGN_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _pause_campaign(reason=''):
    """يعلّم الحملة المحفوظة متوقفة عشان الاستئناف التلقائي ما يعيدش تشغيلها."""
    c = load_campaign()
    if c:
        c['paused'] = True
        c['pause_reason'] = reason
        save_campaign(c)

# ===== Devices & constants =====
def _iph(ua, vw, vh, ver, model, dpr=3.0, name=''):
    return {'ua':ua,'vw':vw,'vh':vh,'dpr':dpr,'name':name,'platform':'iPhone',
            'vendor':'Apple Computer, Inc.','engine':'safari','is_ios':True,
            'ios_ver':ver,'ios_model':model,'cores':6,'mem':None,
            'webgl_vendor':'Apple','webgl_renderer':'Apple GPU'}

def _saf(v):  # Safari iOS UA
    return ('Mozilla/5.0 (iPhone; CPU iPhone OS %s like Mac OS X) AppleWebKit/605.1.15 '
            '(KHTML, like Gecko) Version/%s Mobile/15E148 Safari/604.1'
            % (v.replace('.', '_'), v))

def _and(model, chrome, vw, vh, gpu_v, gpu_r, name, dpr=3.0, mem=8, av='14'):
    major = chrome.split('.')[0]
    return {'ua':('Mozilla/5.0 (Linux; Android %s; %s) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/%s Mobile Safari/537.36' % (av, model, chrome)),
            'vw':vw,'vh':vh,'dpr':dpr,'name':name,'platform':'Linux aarch64',
            'vendor':'Google Inc.','engine':'chrome',
            'ch_ua':'"Chromium";v="%s", "Google Chrome";v="%s", "Not-A.Brand";v="99"' % (major, major),
            'is_ios':False,'cores':8,'mem':mem,'webgl_vendor':gpu_v,'webgl_renderer':gpu_r}

# نسخ كروم كاملة حديثة
_C135, _C134, _C133, _C132, _C131 = ('135.0.7049.83','134.0.6998.135','133.0.6943.137','132.0.6834.163','131.0.6778.104')
_CHROME_FULL = {'135':_C135, '134':_C134, '133':_C133, '132':_C132, '131':_C131}

def _desk(ua, vw, vh, plat, chp, vend, eng, gpu_v, gpu_r, name, dpr=1.0, ch=None):
    """جهاز ديسكتوب — بدون لمس، شاشة كبيرة، بصمة كمبيوتر."""
    d = {'ua':ua, 'vw':vw, 'vh':vh, 'dpr':dpr, 'name':name, 'platform':plat,
         'vendor':vend, 'engine':eng, 'is_ios':False, 'is_desktop':True,
         'ch_platform':chp, 'cores':8, 'mem':8,
         'webgl_vendor':gpu_v, 'webgl_renderer':gpu_r}
    if ch:
        d['ch_ua'] = ch
    return d

def _dchua(kind, major):  # Client Hints brands لسطح المكتب (Chrome أو Edge)
    if kind == 'edge':
        return '"Microsoft Edge";v="%s", "Chromium";v="%s", "Not-A.Brand";v="99"' % (major, major)
    return '"Chromium";v="%s", "Google Chrome";v="%s", "Not-A.Brand";v="99"' % (major, major)

DEVICES = [
    # ===== iPhones (Safari, Apple GPU, 6 cores) =====
    _iph(_saf('18.3'), 393, 852, '18.3', 'iPhone17,3', name='iPhone 16'),
    _iph(_saf('18.3'), 402, 874, '18.3', 'iPhone17,1', name='iPhone 16 Pro'),
    _iph(_saf('18.1'), 440, 956, '18.1', 'iPhone17,2', name='iPhone 16 Pro Max'),
    _iph(_saf('18.3'), 393, 852, '18.3', 'iPhone16,1', name='iPhone 15 Pro'),
    _iph(_saf('18.2'), 430, 932, '18.2', 'iPhone16,2', name='iPhone 15 Pro Max'),
    _iph(_saf('18.1'), 393, 852, '18.1', 'iPhone15,4', name='iPhone 15'),
    _iph(_saf('18.0'), 430, 932, '18.0', 'iPhone15,5', name='iPhone 15 Plus'),
    _iph(_saf('17.7'), 393, 852, '17.7', 'iPhone15,2', name='iPhone 14 Pro'),
    _iph(_saf('18.2'), 430, 932, '18.2', 'iPhone15,3', name='iPhone 14 Pro Max'),
    _iph(_saf('17.6'), 390, 844, '17.6', 'iPhone14,7', name='iPhone 14'),
    _iph(_saf('18.1'), 428, 926, '18.1', 'iPhone14,8', name='iPhone 14 Plus'),
    _iph(_saf('17.7'), 390, 844, '17.7', 'iPhone14,2', name='iPhone 13 Pro'),
    _iph(_saf('18.0'), 390, 844, '18.0', 'iPhone14,5', name='iPhone 13'),
    _iph(_saf('17.6'), 375, 812, '17.6', 'iPhone14,4', name='iPhone 13 mini'),
    _iph(_saf('18.1'), 390, 844, '18.1', 'iPhone13,2', name='iPhone 12'),
    _iph(_saf('17.5'), 375, 667, '17.5', 'iPhone14,6', dpr=2.0, name='iPhone SE 3'),
    # iPhone عبر كروم iOS (CriOS) — لسه Apple GPU، من غير userAgentData
    {'ua':'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/%s Mobile/15E148 Safari/604.1' % _C135,
     'vw':393,'vh':852,'dpr':3.0,'name':'iPhone 15 (Chrome)','platform':'iPhone','vendor':'Google Inc.','engine':'chrome',
     'ch_ua':'"Chromium";v="135", "Google Chrome";v="135", "Not-A.Brand";v="99"','is_ios':True,'ios_ver':'18.1','ios_model':'iPhone15,4','cores':6,'mem':None,'webgl_vendor':'Apple','webgl_renderer':'Apple GPU'},

    # ===== Samsung =====
    _and('SM-S928B', _C135, 412, 915, 'Qualcomm', 'Adreno (TM) 750',        'Galaxy S24 Ultra', dpr=3.5, mem=8),
    _and('SM-S921B', _C134, 360, 780, 'ARM',      'Xclipse 940',            'Galaxy S24',       dpr=3.0, mem=8),
    _and('SM-S918B', _C133, 384, 824, 'Qualcomm', 'Adreno (TM) 740',        'Galaxy S23 Ultra', dpr=3.75, mem=8),
    _and('SM-S911B', _C132, 360, 780, 'Qualcomm', 'Adreno (TM) 740',        'Galaxy S23',       dpr=3.0, mem=8),
    _and('SM-F956B', _C135, 344, 882, 'Qualcomm', 'Adreno (TM) 750',        'Galaxy Z Fold6',   dpr=3.0, mem=8),
    _and('SM-F741B', _C134, 412, 916, 'Qualcomm', 'Adreno (TM) 750',        'Galaxy Z Flip6',   dpr=2.625, mem=8),
    _and('SM-A556B', _C135, 360, 800, 'ARM',      'Xclipse 530',            'Galaxy A55',       dpr=3.0, mem=8),
    _and('SM-A546B', _C134, 360, 780, 'ARM',      'Mali-G68 MC4',           'Galaxy A54',       dpr=3.0, mem=8),
    _and('SM-A356B', _C133, 360, 792, 'ARM',      'Mali-G68 MC4',           'Galaxy A35',       dpr=3.0, mem=6),
    _and('SM-A155F', _C132, 360, 800, 'ARM',      'Mali-G57 MC2',           'Galaxy A15',       dpr=3.0, mem=4, av='14'),

    # ===== Google Pixel (Tensor → Mali) =====
    _and('Pixel 9 Pro', _C135, 412, 915, 'ARM', 'Mali-G715',                'Pixel 9 Pro', dpr=3.5, mem=8, av='15'),
    _and('Pixel 9',     _C134, 412, 915, 'ARM', 'Mali-G715',                'Pixel 9',     dpr=2.625, mem=8, av='15'),
    _and('Pixel 8 Pro', _C133, 412, 915, 'ARM', 'Mali-G715',                'Pixel 8 Pro', dpr=2.625, mem=8, av='14'),
    _and('Pixel 8',     _C132, 412, 915, 'ARM', 'Mali-G715',                'Pixel 8',     dpr=2.625, mem=8, av='14'),
    _and('Pixel 7',     _C131, 412, 915, 'ARM', 'Mali-G710',                'Pixel 7',     dpr=2.625, mem=8, av='14'),

    # ===== Xiaomi / Redmi / POCO =====
    _and('23127PN0CG', _C135, 393, 873, 'Qualcomm', 'Adreno (TM) 750',      'Xiaomi 14',        dpr=2.75, mem=8),
    _and('24069PC21G', _C134, 393, 873, 'ARM',      'Mali-G615 MC6',        'Xiaomi 14T',       dpr=2.75, mem=8),
    _and('2312DRA50G', _C133, 393, 873, 'Qualcomm', 'Adreno (TM) 710',      'Redmi Note 13 Pro',dpr=2.75, mem=8),
    _and('23129RAA4G', _C132, 393, 873, 'Qualcomm', 'Adreno (TM) 613',      'Redmi Note 13',    dpr=2.75, mem=6),
    _and('24069PC21I', _C134, 393, 873, 'ARM',      'Mali-G615 MC6',        'POCO X6 Pro',      dpr=2.75, mem=8),

    # ===== OnePlus / OPPO / Realme / Vivo =====
    _and('CPH2581', _C135, 412, 919, 'Qualcomm', 'Adreno (TM) 750',         'OnePlus 12',   dpr=3.5, mem=8),
    _and('CPH2609', _C134, 412, 892, 'ARM',      'Mali-G615 MC2',           'OPPO Reno12',  dpr=2.625, mem=8),
    _and('RMX3842', _C133, 393, 873, 'Qualcomm', 'Adreno (TM) 710',         'Realme 12 Pro',dpr=2.75, mem=8),
    _and('V2318',   _C132, 392, 848, 'Qualcomm', 'Adreno (TM) 720',         'Vivo V30',     dpr=3.0, mem=8),
    _and('V2247',   _C131, 360, 800, 'Qualcomm', 'Adreno (TM) 610',         'Vivo Y36',     dpr=3.0, mem=4, av='13'),

    # ===== Motorola / أجهزة أسواق ناشئة =====
    _and('motorola edge 50', _C134, 384, 854, 'Qualcomm', 'Adreno (TM) 720','Moto Edge 50', dpr=2.75, mem=8),
    _and('XT2347-2', _C133, 393, 873, 'Qualcomm', 'Adreno (TM) 619',        'Moto G84',     dpr=2.75, mem=8),
    _and('TECNO CK6n', _C132, 360, 800, 'ARM', 'Mali-G52 MC2',              'Tecno Camon 20', dpr=3.0, mem=8, av='13'),
    _and('Infinix X6833B', _C131, 360, 800, 'ARM', 'Mali-G57 MC2',          'Infinix Note 30', dpr=3.0, mem=8, av='13'),

    # ===== Desktop (بصمة كمبيوتر — بدون لمس، شاشة كبيرة، Client Hints ديسكتوب) =====
    _desk('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
          1536, 864, 'Win32', 'Windows', 'Google Inc.', 'chrome',
          'Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)',
          'Windows Chrome', dpr=1.0, ch=_dchua('chrome', '135')),
    _desk('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
          1920, 1080, 'Win32', 'Windows', 'Google Inc.', 'chrome',
          'Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)',
          'Windows Chrome', dpr=1.0, ch=_dchua('chrome', '134')),
    _desk('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0',
          1366, 768, 'Win32', 'Windows', 'Google Inc.', 'chrome',
          'Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A6) Direct3D11 vs_5_0 ps_5_0, D3D11)',
          'Windows Edge', dpr=1.0, ch=_dchua('edge', '135')),
    _desk('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
          1440, 900, 'MacIntel', 'macOS', 'Google Inc.', 'chrome',
          'Google Inc. (Apple)', 'ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)',
          'Mac Chrome', dpr=2.0, ch=_dchua('chrome', '135')),
    _desk('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
          1440, 900, 'MacIntel', 'macOS', 'Apple Computer, Inc.', 'safari',
          'Apple GPU', 'Apple GPU', 'Mac Safari', dpr=2.0),   # Safari = بدون Client Hints
]

# فصل الأجهزة حسب النوع (تُستخدم كـ fallback فقط — التوليد الحي تحت)
_ANDROID_DEVS = [d for d in DEVICES if not d.get('is_ios') and not d.get('is_desktop')]
_IOS_DEVS     = [d for d in DEVICES if d.get('is_ios')]
_DESKTOP_DEVS = [d for d in DEVICES if d.get('is_desktop')]

# ============================================================
# مولّد البصمات: ملايين البصمات الواقعية (موبايل + ديسكتوب)
# ------------------------------------------------------------
# كل جلسة بتولّد جهاز جديد بتركيبة عشوائية *متسقة وحقيقية*:
#   موديل × نسخة أندرويد × بيلد كروم × شاشة × GPU × RAM/أنوية
# الضرب التوافقي = ملايين التركيبات، وكل قيمة منها جهاز حقيقي ممكن.
# + ضوضاء canvas/WebGL/audio لكل جلسة (NOISE_SEED) = مفيش جلستين
#   ليهم نفس البصمة المرسومة، حتى لو نفس الجهاز المُعلن.
# القيود المحفوظة: deviceMemory ∈ {4,8} فقط (الحد الأقصى للمتصفح 8)،
#   iOS كله Safari (iOS Chrome بيبان Chromium — تجنبناه)، الديسكتوب
#   بلا لمس، Client Hints مشتقة من الـ UA دايمًا.

# بيلدات كروم كاملة حقيقية حديثة (نطاق واسع = تنوّع في الـ UA)
_CHROME_POOL = [
    '135.0.7049.100','135.0.7049.85','135.0.7049.52','135.0.7049.83',
    '134.0.6998.135','134.0.6998.89','134.0.6998.45',
    '133.0.6943.142','133.0.6943.127','133.0.6943.54','133.0.6943.137',
    '132.0.6834.163','132.0.6834.122','132.0.6834.84',
    '131.0.6778.140','131.0.6778.104','131.0.6778.86',
    '130.0.6723.116','130.0.6723.92',
]

# عائلات أندرويد: كل عائلة = موديلات + GPUات + نسخ أندرويد + شاشات + رام حقيقية
_ANDROID_FAMILIES = [
    {'name':'Galaxy', 'models':['SM-S928B','SM-S926B','SM-S921B','SM-S918B','SM-S911B','SM-S908B','SM-F956B','SM-F741B','SM-A556B','SM-A546B','SM-A356B'],
     'gpus':[('Qualcomm','Adreno (TM) 750'),('Qualcomm','Adreno (TM) 740'),('ARM','Xclipse 940'),('ARM','Xclipse 920'),('ARM','Mali-G68 MC4')],
     'av':['13','14','15'], 'screens':[(412,915,3.5),(360,780,3.0),(384,824,3.75),(360,800,3.0)], 'mem':[8,8,4]},
    {'name':'Pixel', 'models':['Pixel 9 Pro','Pixel 9','Pixel 8 Pro','Pixel 8','Pixel 7 Pro','Pixel 7'],
     'gpus':[('ARM','Mali-G715'),('ARM','Mali-G710')],
     'av':['14','15'], 'screens':[(412,915,3.5),(412,915,2.625),(393,851,2.75)], 'mem':[8,8]},
    {'name':'Xiaomi', 'models':['23127PN0CG','24069PC21G','2312DRA50G','23129RAA4G','24069PC21I','2311DRK48G'],
     'gpus':[('Qualcomm','Adreno (TM) 750'),('Qualcomm','Adreno (TM) 710'),('Qualcomm','Adreno (TM) 613'),('ARM','Mali-G615 MC6')],
     'av':['13','14','15'], 'screens':[(393,873,2.75),(360,800,3.0),(412,915,2.625)], 'mem':[8,8,4]},
    {'name':'OnePlus', 'models':['CPH2581','CPH2609','CPH2449','RMX3842'],
     'gpus':[('Qualcomm','Adreno (TM) 750'),('Qualcomm','Adreno (TM) 730'),('ARM','Mali-G615 MC2')],
     'av':['14','15'], 'screens':[(412,919,3.5),(412,892,2.625),(393,873,2.75)], 'mem':[8,8]},
    {'name':'OPPO', 'models':['CPH2609','CPH2437','CPH2557'],
     'gpus':[('Qualcomm','Adreno (TM) 720'),('ARM','Mali-G615 MC2'),('Qualcomm','Adreno (TM) 710')],
     'av':['13','14'], 'screens':[(360,804,3.0),(393,873,2.75)], 'mem':[8,4]},
    {'name':'Vivo', 'models':['V2324','V2318','V2247','V2250'],
     'gpus':[('Qualcomm','Adreno (TM) 720'),('Qualcomm','Adreno (TM) 610'),('ARM','Mali-G57 MC2')],
     'av':['13','14'], 'screens':[(392,848,3.0),(360,800,3.0)], 'mem':[8,4]},
    {'name':'Moto', 'models':['motorola edge 50','XT2347-2','XT2451-4'],
     'gpus':[('Qualcomm','Adreno (TM) 720'),('Qualcomm','Adreno (TM) 619')],
     'av':['13','14'], 'screens':[(384,854,2.75),(393,873,2.75)], 'mem':[8,4]},
    {'name':'Android', 'models':['TECNO CK6n','Infinix X6833B','TECNO CI8n','Infinix X6739'],
     'gpus':[('ARM','Mali-G57 MC2'),('ARM','Mali-G52 MC2')],
     'av':['13','14'], 'screens':[(360,800,3.0),(360,780,2.0)], 'mem':[8,4]},
]

def _gen_android():
    fam = random.choice(_ANDROID_FAMILIES)
    gpu_v, gpu_r = random.choice(fam['gpus'])
    vw, vh, dpr  = random.choice(fam['screens'])
    d = _and(random.choice(fam['models']), random.choice(_CHROME_POOL),
             vw, vh, gpu_v, gpu_r, fam['name'],
             dpr=dpr, mem=random.choice(fam['mem']), av=random.choice(fam['av']))
    d['cores'] = random.choice([8, 8, 8, 6])   # أغلب الموبايلات ثماني الأنوية
    return d

# موديلات آيفون حقيقية (كلها Safari — iOS Chrome بيبان Chromium فتجنبناه)
_IOS_MODELS = [
    ('iPhone17,3',393,852,3.0,'iPhone 16'),      ('iPhone17,1',402,874,3.0,'iPhone 16 Pro'),
    ('iPhone17,2',440,956,3.0,'iPhone 16 Pro Max'),('iPhone16,1',393,852,3.0,'iPhone 15 Pro'),
    ('iPhone16,2',430,932,3.0,'iPhone 15 Pro Max'),('iPhone15,4',393,852,3.0,'iPhone 15'),
    ('iPhone15,5',430,932,3.0,'iPhone 15 Plus'), ('iPhone15,2',393,852,3.0,'iPhone 14 Pro'),
    ('iPhone15,3',430,932,3.0,'iPhone 14 Pro Max'),('iPhone14,7',390,844,3.0,'iPhone 14'),
    ('iPhone14,8',428,926,3.0,'iPhone 14 Plus'), ('iPhone14,2',390,844,3.0,'iPhone 13 Pro'),
    ('iPhone14,5',390,844,3.0,'iPhone 13'),      ('iPhone13,2',390,844,3.0,'iPhone 12'),
    ('iPhone14,6',375,667,2.0,'iPhone SE 3'),
]
_IOS_VER_POOL = ['18.3','18.2','18.1','18.0','17.7','17.6','17.5']

def _gen_ios():
    model, vw, vh, dpr, name = random.choice(_IOS_MODELS)
    ver = random.choice(_IOS_VER_POOL)
    return _iph(_saf(ver), vw, vh, ver, model, dpr=dpr, name=name)

# ديسكتوب: GPUات + دقّات شاشة حقيقية لكل نظام
_WIN_GPUS = [
    ('Google Inc. (Intel)','ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (Intel)','ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A6) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (Intel)','ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00003EA0) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (NVIDIA)','ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 (0x00001F91) Direct3D11 vs_5_0 ps_5_0, D3D11)'),
    ('Google Inc. (AMD)','ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'),
]
_WIN_RES  = [(1920,1080,1.0),(1536,864,1.25),(1366,768,1.0),(1600,900,1.0),(2560,1440,1.0),(1280,720,1.0)]
_MAC_GPUS = [
    ('Google Inc. (Apple)','ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)'),
    ('Google Inc. (Apple)','ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)'),
    ('Google Inc. (Apple)','ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)'),
]
_MAC_RES  = [(1440,900,2.0),(1512,982,2.0),(1280,800,2.0),(1728,1117,2.0)]
_MAC_SAFARI_VERS = ['17.4.1','17.5','17.6','18.0','18.1','18.2']

def _gen_desktop():
    chrome = random.choice(_CHROME_POOL); major = chrome.split('.')[0]
    if random.random() < 0.72:                       # ويندوز
        vw, vh, dpr = random.choice(_WIN_RES)
        gpu_v, gpu_r = random.choice(_WIN_GPUS)
        if random.random() < 0.25:                   # Edge
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/%s Safari/537.36 Edg/%s' % (chrome, chrome))
            ch = _dchua('edge', major)
        else:                                        # Chrome
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/%s Safari/537.36' % chrome)
            ch = _dchua('chrome', major)
        d = _desk(ua, vw, vh, 'Win32', 'Windows', 'Google Inc.', 'chrome', gpu_v, gpu_r, 'Windows', dpr=dpr, ch=ch)
    else:                                            # ماك
        vw, vh, dpr = random.choice(_MAC_RES)
        if random.random() < 0.45:                   # Safari (بدون Client Hints)
            sv = random.choice(_MAC_SAFARI_VERS)
            ua = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) '
                  'Version/%s Safari/605.1.15' % sv)
            d = _desk(ua, vw, vh, 'MacIntel', 'macOS', 'Apple Computer, Inc.', 'safari', 'Apple GPU', 'Apple GPU', 'Mac Safari', dpr=dpr)
        else:                                        # Chrome
            gpu_v, gpu_r = random.choice(_MAC_GPUS)
            ua = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/%s Safari/537.36' % chrome)
            d = _desk(ua, vw, vh, 'MacIntel', 'macOS', 'Google Inc.', 'chrome', gpu_v, gpu_r, 'Mac Chrome', dpr=dpr, ch=_dchua('chrome', major))
    d['cores'] = random.choice([4, 6, 8, 8, 12, 16])
    d['mem']   = random.choice([8, 8, 4])            # deviceMemory محدود بـ 8 كحد أقصى
    return d

# locale + timezone matched per proxy country
COUNTRY_PROFILES = {
    'gb': {'locales': ['en-GB','en'],        'tz': 'Europe/London'},
    'us': {'locales': ['en-US','en'],        'tz': 'America/New_York'},
    'ca': {'locales': ['en-CA','fr-CA','en'],'tz': 'America/Toronto'},
    'au': {'locales': ['en-AU','en'],        'tz': 'Australia/Sydney'},
    'de': {'locales': ['de-DE','de','en'],   'tz': 'Europe/Berlin'},
    'fr': {'locales': ['fr-FR','fr','en'],   'tz': 'Europe/Paris'},
    'nl': {'locales': ['nl-NL','nl','en'],   'tz': 'Europe/Amsterdam'},
    'it': {'locales': ['it-IT','it','en'],   'tz': 'Europe/Rome'},
    'es': {'locales': ['es-ES','es','en'],   'tz': 'Europe/Madrid'},
    'se': {'locales': ['sv-SE','sv','en'],   'tz': 'Europe/Stockholm'},
    'sa': {'locales': ['ar-SA','ar','en'],   'tz': 'Asia/Riyadh'},
    'pk': {'locales': ['en-PK','ur-PK','en'],'tz': 'Asia/Karachi'},
    'eg': {'locales': ['ar-EG','ar','en'],   'tz': 'Africa/Cairo'},
    'default': {'locales': ['en-US','en'],   'tz': 'America/New_York'},  # افتراضي إنجليزي للترافيك الغربي
}

LOCALES   = ['ar-EG','ar-SA','ar-AE','ar-JO','ar-MA']

# مصادر حسب اللغة: en=English markets, ar=Arabic markets
TRAFFIC_SOURCES_EN = [
    # ── إعلانات فيسبوك (paid) ~40% ──
    ('https://l.facebook.com/',    'facebook',  'paid_social', 'fb_ads'),
    ('https://www.facebook.com/',  'facebook',  'paid_social', 'fb_ads'),
    ('https://l.facebook.com/',    'facebook',  'paid_social', 'fb_ads'),
    ('https://m.facebook.com/',    'facebook',  'paid_social', 'fb_ads'),
    # ── منشور فيسبوك (organic) ~30% ──
    ('https://m.facebook.com/',    'facebook',  'social',      'fb_post'),
    ('https://l.facebook.com/',    'facebook',  'social',      'fb_post'),
    ('https://www.facebook.com/',  'facebook',  'social',      'fb_post'),
    # ── رسائل ماسنجر ~30% ──
    ('https://l.facebook.com/',    'messenger', 'social',      'fb_message'),
    ('https://l.messenger.com/',   'messenger', 'social',      'fb_message'),
    ('https://l.facebook.com/',    'messenger', 'social',      'fb_message'),
]
# الفيسبوك واحد في كل اللغات — نفس القنوات للعربي والأجنبي
TRAFFIC_SOURCES_AR = TRAFFIC_SOURCES_EN
# legacy alias
TRAFFIC_SOURCES = TRAFFIC_SOURCES_EN

UTM_CONTENTS = ['feed','story','reel','post','link','bio','ad']

# fbclid: معرّف نقرة فيسبوك — يُضاف على كل زيارة عشان تبان طبيعية جاية من الفيس
_FBCLID_CHARS    = string.ascii_letters + string.digits + '-_'
_FBCLID_PREFIXES = ['IwY2xjaw', 'IwZXh0bgNhZW0', 'IwAR1']   # صيغ حديثة + قديمة
def _fbclid():
    pre = random.choice(_FBCLID_PREFIXES)
    return pre + ''.join(random.choice(_FBCLID_CHARS) for _ in range(random.randint(40, 58)))

# UA تطبيق فيسبوك/ماسنجر: أندرويد FB4A/Orca-Android ، آيفون FBIOS/MessengerLite
_FB_AND_VERS = ['449.0.0.35.108', '452.0.0.41.109', '455.0.0.49.60', '458.0.0.36.109']
_FB_IOS_VERS = ['449.0.0.35.121', '452.0.0.41.120', '455.0.0.49.70', '458.0.0.36.118']
def _fb_app_ua(base_ua, is_messenger=False, is_ios=False, ios_ver='18.3', ios_model='iPhone15,2', dpr=3.0):
    if is_ios:
        v   = random.choice(_FB_IOS_VERS)
        app = 'MessengerLite' if is_messenger else 'FBIOS'
        ss  = max(1, int(dpr))
        return (base_ua + f' [FBAN/{app};FBDV/{ios_model};FBMD/iPhone;FBSN/iOS;'
                f'FBSV/{ios_ver};FBSS/{ss};FBID/phone;FBLC/ar_AR;FBOP/5]')
    v   = random.choice(_FB_AND_VERS)
    app = 'Orca-Android' if is_messenger else 'FB4A'
    return base_ua + f' [FB_IAB/{app};FBAV/{v};IABMV/1]'

# === Client Hints متطابقة مع الـ UA (#1) ===
# أندرويد كروم بس بيبعت Client Hints. iOS (Safari وCriOS) مالهاش userAgentData ولا CH.
def _client_hints(dev):
    """يبني هيدرز Sec-CH-UA-* + بيانات userAgentData متطابقة مع UA الجهاز."""
    if dev.get('is_ios') or dev.get('engine') != 'chrome' or not dev.get('ch_ua'):
        return {}, None
    import re
    # --- فرع الديسكتوب: Mobile ?0 + منصة Windows/macOS + بدون موديل ---
    if dev.get('is_desktop'):
        major = re.search(r'v="(\d+)"', dev['ch_ua'])
        major = major.group(1) if major else '135'
        cver  = _CHROME_FULL.get(major, major + '.0.0.0')
        plat  = dev.get('ch_platform', 'Windows')
        pv    = '15.0.0' if plat == 'Windows' else '14.4.0'
        brands = [{'brand': m.group(1), 'version': m.group(2)}
                  for m in re.finditer(r'"([^"]+)";v="([^"]+)"', dev['ch_ua'])]
        fvl = [{'brand': b['brand'],
                'version': ('99.0.0.0' if ('Not' in b['brand'] or 'Brand' in b['brand']) else cver)}
               for b in brands]
        fvl_hdr = ', '.join(f'"{b["brand"]}";v="{b["version"]}"' for b in fvl)
        headers = {
            'Sec-CH-UA':                   dev['ch_ua'],
            'Sec-CH-UA-Mobile':            '?0',
            'Sec-CH-UA-Platform':          f'"{plat}"',
            'Sec-CH-UA-Model':             '""',
            'Sec-CH-UA-Platform-Version':  f'"{pv}"',
            'Sec-CH-UA-Full-Version-List': fvl_hdr,
            'Sec-CH-UA-Arch':              '"x86"',
            'Sec-CH-UA-Bitness':           '"64"',
        }
        uad = {'brands': brands, 'mobile': False, 'platform': plat, 'model': '',
               'platformVersion': pv, 'uaFullVersion': cver, 'fullVersionList': fvl,
               'architecture': 'x86', 'bitness': '64'}
        return headers, uad
    ua    = dev['ua']
    m_av  = re.search(r'Android (\d+)', ua)
    m_md  = re.search(r'Android \d+; ([^)]+)\)', ua)
    m_cv  = re.search(r'Chrome/([\d.]+)', ua)
    av    = m_av.group(1) if m_av else '14'
    model = m_md.group(1).strip() if m_md else ''
    cver  = m_cv.group(1) if m_cv else '120.0.0.0'
    pv    = f'{av}.0.0'
    brands = []
    for part in dev['ch_ua'].split(','):
        pm = re.search(r'"([^"]+)";v="([^"]+)"', part)
        if pm:
            brands.append({'brand': pm.group(1), 'version': pm.group(2)})
    fvl = []
    for b in brands:
        is_mask = ('Not' in b['brand']) or ('Brand' in b['brand'])
        fvl.append({'brand': b['brand'], 'version': ('99.0.0.0' if is_mask else cver)})
    fvl_hdr = ', '.join(f'"{b["brand"]}";v="{b["version"]}"' for b in fvl)
    headers = {
        'Sec-CH-UA':                   dev['ch_ua'],
        'Sec-CH-UA-Mobile':            '?1',
        'Sec-CH-UA-Platform':          '"Android"',
        'Sec-CH-UA-Model':             f'"{model}"',
        'Sec-CH-UA-Platform-Version':  f'"{pv}"',
        'Sec-CH-UA-Full-Version-List': fvl_hdr,
        'Sec-CH-UA-Arch':              '""',
        'Sec-CH-UA-Bitness':           '""',
    }
    uad = {'brands': brands, 'mobile': True, 'platform': 'Android',
           'model': model, 'platformVersion': pv, 'uaFullVersion': cver,
           'fullVersionList': fvl, 'architecture': '', 'bitness': ''}
    return headers, uad

PROXY_RETRIES = 2   # محاولات فتح إضافية ببروكسي تالي عند الفشل (بدون شطب أي بروكسي)

# === توفير بيانات البروكسي (وضع آمن للإعلانات) ===
# نمنع الخطوط + الفيديو/الصوت بس — مش الصور ولا الـiframes.
# كده الإعلانات (صور/iframes) بتظهر وتتعرض عادي → الظهور يتحسب والـviewability تعدّي،
# والموقع بيشوفك بتشوف الإعلانات طبيعي. نوفّر الخطوط والميديا التقيلة اللي مالهاش لازمة.
DATA_SAVER   = True
_BLOCK_TYPES = ('image', 'media', 'font')   # حجب قوي — الظهور popunder بيتحسب بالـJS مش بالصور

# === تبطيء النت (سرعة موبايل واقعية) ===
# يقلّل كام يتحمّل من العروض المتجددة في نفس الوقت = توفير داتا + قعدة أطول + يبان موبايل حقيقي.
NET_THROTTLE    = True
THROTTLE_DOWN   = 90_000    # bytes/s ≈ 0.72 ميجابت (نت 4G بطيء واقعي)
THROTTLE_UP     = 30_000
THROTTLE_LAT    = 160       # ms زمن استجابة موبايل

async def _apply_throttle(page):
    if not NET_THROTTLE:
        return
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send('Network.enable')
        await cdp.send('Network.emulateNetworkConditions', {
            'offline': False, 'latency': THROTTLE_LAT,
            'downloadThroughput': THROTTLE_DOWN, 'uploadThroughput': THROTTLE_UP,
        })
    except Exception:
        pass

def _build_url(base_url, traffic_mix, locale='en'):
    """يبني الزيارة كأنها جاية من فيسبوك (إعلان/منشور/رسالة) مع fbclid وUTM"""
    if not traffic_mix:
        return base_url, random.choice(['https://l.facebook.com/', 'https://m.facebook.com/'])
    pool = TRAFFIC_SOURCES_AR if locale.startswith('ar') else TRAFFIC_SOURCES_EN
    src = random.choice(pool)
    referer, utm_src, utm_med, utm_camp = src
    if not utm_src:
        return base_url, referer
    content = random.choice(UTM_CONTENTS)
    sep     = '&' if '?' in base_url else '?'
    final   = (f'{base_url}{sep}utm_source={utm_src}'
               f'&utm_medium={utm_med}'
               f'&utm_campaign={utm_camp}'
               f'&utm_content={content}'
               f'&fbclid={_fbclid()}')
    return final, referer
STEALTH_JS = """
(function(){
const _ios=__IS_IOS__, _safari=__IS_SAFARI__;
const _wv='__WEBGL_VENDOR__',_wr='__WEBGL_RENDERER__';

// --- webdriver ---
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});

// --- plugins: فاضي على Safari (موبايل/ماك)، قائمة على Chrome ---
Object.defineProperty(navigator,'plugins',{get:()=>_safari?[]:[{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]});

// --- languages + locale ---
Object.defineProperty(navigator,'language',{get:()=>'__LOCALE__'});
Object.defineProperty(navigator,'languages',{get:()=>['__LOCALE__','__LOCALE2__','en']});

// --- platform per device ---
Object.defineProperty(navigator,'platform',{get:()=>'__PLATFORM__'});

// --- vendor per engine ---
Object.defineProperty(navigator,'vendor',{get:()=>'__VENDOR__'});

// --- hardware concurrency: device-accurate constant ---
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>__CORES__});

// --- device memory: Chrome فقط (Safari موبايل/ماك مالهاش deviceMemory) ---
if(!_safari){try{Object.defineProperty(navigator,'deviceMemory',{get:()=>__MEM__});}catch(e){}}
else{try{Object.defineProperty(navigator,'deviceMemory',{get:()=>undefined,configurable:true});}catch(e){}}

// --- touch: 5 للموبايل، 0 للديسكتوب ---
Object.defineProperty(navigator,'maxTouchPoints',{get:()=>__MAXTOUCH__});

// --- chrome runtime (chrome engine only, not iOS Safari) ---
if('__ENGINE__'==='chrome'){
  window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
}

// --- navigator.userAgentData: متطابق مع UA (أندرويد) أو غير موجود (iOS/Safari) (#1) ---
(function(){
  const _uad=__UAD__;
  if(_uad){
    const he={architecture:_uad.architecture,bitness:_uad.bitness,model:_uad.model,
              platformVersion:_uad.platformVersion,uaFullVersion:_uad.uaFullVersion,
              fullVersionList:_uad.fullVersionList,brands:_uad.brands,
              mobile:_uad.mobile,platform:_uad.platform,wow64:false};
    const low={brands:_uad.brands,mobile:_uad.mobile,platform:_uad.platform};
    const uaData={brands:_uad.brands,mobile:_uad.mobile,platform:_uad.platform,
      getHighEntropyValues:(hints)=>Promise.resolve((()=>{
        const o=Object.assign({},low);
        (hints||[]).forEach(h=>{ if(h in he) o[h]=he[h]; });
        return o;
      })()),
      toJSON:()=>Object.assign({},low)};
    try{Object.defineProperty(navigator,'userAgentData',{get:()=>uaData,configurable:true});}catch(e){}
  } else {
    // iOS / Safari: الخاصية دي مالهاش وجود أصلاً
    try{Object.defineProperty(navigator,'userAgentData',{get:()=>undefined,configurable:true});}catch(e){}
  }
})();

// --- permissions ---
try{
  const _orig=window.navigator.permissions&&window.navigator.permissions.query;
  if(_orig){window.navigator.permissions.query=(p)=>p.name==='notifications'?Promise.resolve({state:'denied'}):_orig.call(window.navigator.permissions,p);}
}catch(e){}

// --- WebRTC leak block ---
['RTCPeerConnection','webkitRTCPeerConnection'].forEach(k=>{
  if(!window[k]) return;
  const _C=window[k];
  window[k]=function(cfg){if(cfg&&cfg.iceServers)cfg.iceServers=[];return new _C(cfg);};
  window[k].prototype=_C.prototype;
});

// --- Canvas noise: per-session seed __NOISE_SEED__ (1-254) ---
const _seed=__NOISE_SEED__;
const _toDataURL=HTMLCanvasElement.prototype.toDataURL;
const _getImageData=CanvasRenderingContext2D.prototype.getImageData;
HTMLCanvasElement.prototype.toDataURL=function(){
  const ctx=this.getContext('2d');
  if(ctx&&this.width>0&&this.height>0){
    const img=_getImageData.call(ctx,0,0,this.width,this.height);
    for(let i=0;i<img.data.length;i+=17)img.data[i]^=_seed;
    ctx.putImageData(img,0,0);
  }
  return _toDataURL.apply(this,arguments);
};

// --- WebGL: device-accurate vendor + renderer ---
if(window.WebGLRenderingContext){
  const _g=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){if(p===37445)return _wv;if(p===37446)return _wr;return _g.call(this,p);};
}
if(window.WebGL2RenderingContext){
  const _g2=WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter=function(p){if(p===37445)return _wv;if(p===37446)return _wr;return _g2.call(this,p);};
}

// --- Screen match viewport ---
Object.defineProperty(screen,'width',{get:()=>window.innerWidth});
Object.defineProperty(screen,'height',{get:()=>window.innerHeight});
Object.defineProperty(screen,'availWidth',{get:()=>window.innerWidth});
Object.defineProperty(screen,'availHeight',{get:()=>window.innerHeight-50});
Object.defineProperty(screen,'colorDepth',{get:()=>24});
Object.defineProperty(screen,'pixelDepth',{get:()=>24});

// --- navigator.connection: Chrome فقط (Safari مالهاش Network Information API) ---
if(!_safari){
  try{
    const conn={effectiveType:'4g',downlink:Math.round((5+Math.random()*45)*10)/10,
                rtt:[50,80,100][Math.floor(Math.random()*3)],
                saveData:false,onchange:null,addEventListener:()=>{},removeEventListener:()=>{}};
    Object.defineProperty(navigator,'connection',{get:()=>conn});
    Object.defineProperty(navigator,'mozConnection',{get:()=>conn});
  }catch(e){}
} else {
  // Safari: نشيل navigator.connection فعلياً (Chromium بيحطها أصلاً)
  try{Object.defineProperty(navigator,'connection',{get:()=>undefined,configurable:true});}catch(e){}
  try{Object.defineProperty(navigator,'mozConnection',{get:()=>undefined,configurable:true});}catch(e){}
}

// --- Battery API ---
if(navigator.getBattery){
  const lvl=Math.round((0.3+Math.random()*0.6)*100)/100;
  navigator.getBattery=()=>Promise.resolve({charging:Math.random()>0.6,chargingTime:Infinity,
    dischargingTime:Math.floor(Math.random()*7200)+1800,level:lvl,addEventListener:()=>{},removeEventListener:()=>{}});
}

// --- Performance timing noise ---
const _now=performance.now.bind(performance);
performance.now=()=>_now()+Math.random()*0.5;
})();
"""

# ===== Human simulation helpers =====
async def _human_move(page, tx, ty):
    """تحريك موس تدريجي بمنحنى smoothstep + ارتجاج عشوائي"""
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
    """تمرير تدريجي — لأعلى أو أسفل"""
    steps  = max(2, abs(px) // 70)
    per    = px // steps
    for _ in range(steps):
        await page.evaluate(f'window.scrollBy(0, {per})')
        await asyncio.sleep(random.uniform(0.05, 0.18))

async def _get_clickables(page):
    """أول 50 عنصر قابل للضغط وظاهر مع إحداثياته"""
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

AD_CLICK_RATE = 0.0    # موقع popunder/CPM — النقر مش محتاجه (يوفّر داتا + أأمن)

# محدّدات الإعلانات — شبكات + iframes + روابط مباشرة (سمارت-لينك)
AD_SELECTORS = [
    'iframe[src*="ads"]', 'iframe[src*="/ad"]', 'iframe[src*="banner"]', 'iframe[src*="pop"]',
    'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]', 'iframe[src*="adservice"]',
    'iframe[src*="adnxs"]', 'iframe[src*="amazon-adsystem"]', 'iframe[src*="/afu"]',
    'iframe[id*="google_ads"]', 'iframe[id*="aswift"]', 'iframe[id*="ad"]',
    'iframe[aria-label*="Advertisement"]', 'iframe[title*="Advertisement"]',
    'ins.adsbygoogle', '[id*="ad-container"]', '[id*="ad_container"]', '[id*="ad-slot"]',
    '[id*="banner"]', '[class*="ad-wrap"]', '[class*="ad-slot"]', '[class*="ad-unit"]',
    '[class*="advertisement"]', '[class*="sponsored"]', '[data-ad]', '[data-ad-slot]',
    'a[href*="adsterra"]', 'a[href*="propeller"]', 'a[href*="aff"]', 'a[href*="/smartlink"]',
    'a[href*="/redirect"]', 'a[href*="/click"]', 'a[target="_blank"][href^="http"]', 'iframe',
]

async def _find_ad(page):
    """يرجّع أول إعلان مرئي مناسب (element, box) أو None."""
    for sel in AD_SELECTORS:
        try:
            els = await page.query_selector_all(sel)
        except Exception:
            continue
        for el in els[:6]:
            try:
                if not await el.is_visible():
                    continue
                b = await el.bounding_box()
                if not b or b['width'] < 30 or b['height'] < 20:
                    continue
                if b['width'] > 1200 and b['height'] > 1500:   # عنصر ضخم = مش إعلان غالباً
                    continue
                return el, b
            except Exception:
                pass
    return None

async def _click_box(page, b):
    """ضغطة إنسانية على صندوق العنصر."""
    await page.evaluate("window.scrollTo({top: %d, behavior:'smooth'})" % max(0, int(b['y'] - 140)))
    await asyncio.sleep(random.uniform(0.8, 2.0))
    try:
        b2 = b  # بعد السكرول الإحداثيات النسبية للـviewport تقريبية
    except Exception:
        b2 = b
    x = b2['x'] + b2['width']  * random.uniform(0.3, 0.7)
    y = b2['y'] + b2['height'] * random.uniform(0.3, 0.7)
    # نضبط y داخل الـviewport بعد السكرول
    try:
        vh = await page.evaluate('window.innerHeight')
        if y > vh: y = vh * random.uniform(0.4, 0.7)
    except Exception:
        pass
    await _human_move(page, x, max(10, y))
    await asyncio.sleep(random.uniform(0.3, 0.9))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.16))
    await page.mouse.up()
    await asyncio.sleep(random.uniform(1.5, 4.0))

async def _try_click_ad(page):
    """يدوّر على إعلان — بيسكرول ويعيد المحاولة عشان الإعلانات اللي بتحمّل متأخر."""
    for attempt in range(3):
        hit = await _find_ad(page)
        if hit:
            try:
                await _click_box(page, hit[1])
                return True
            except Exception:
                pass
        # مفيش إعلان لسه؟ اسكرول شوية عشان الإعلانات الكسولة تظهر، وجرّب تاني
        try:
            await _human_scroll(page, random.randint(250, 550))
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.8, 1.8))
    return False

# ===== Browser session =====
async def run_session(playwright, url, proxy, duration, sid, jitter, traffic_mix=True, goto_timeout=90000, pick_proxy=None, require_proxy=False):
    if jitter > 0:
        await asyncio.sleep(random.uniform(0, jitter))

    # شخصية الجلسة (#4): تحدّد ميل الأفعال + مكان المدة داخل المدى
    # المدى = من نص الإعداد لكامله (إعداد 40 → مدى 20-40 ث)، والشخصية بتحدّد المكان
    persona, _pmn, _pmx = random.choice(PERSONAS)
    d_max = max(8.0, float(duration))
    d_min = d_max * 0.5
    span  = d_max - d_min
    if persona == 'bouncer':          # سريع → النص الأدنى من المدى
        duration = random.uniform(d_min, d_min + span * 0.5)
    elif persona == 'reader':         # قارئ → النص الأعلى من المدى
        duration = random.uniform(d_min + span * 0.5, d_max)
    else:                             # ماسح → أي مكان في المدى
        duration = random.uniform(d_min, d_max)

    # زائر عائد؟ (#2) — يبدأ من كوكيز محفوظة عشان يبان returning للموقع
    reuse_state = _pick_ctx_state() if random.random() < RETURNING_RATE else None

    # اختيار الجهاز: بصمة *مولّدة حيًّا* لكل جلسة (ملايين التركيبات الواقعية)
    # موزونة لجمهور فيسبوك مايل للمربح:
    # ~72% أندرويد (Chrome Mobile الأعلى CPM) + ~18% آيفون + ~10% ديسكتوب.
    _r = random.random()
    try:
        if _r < 0.10:
            dev = _gen_desktop()
        elif _r < 0.28:
            dev = _gen_ios()
        else:
            dev = _gen_android()
    except Exception:                      # أمان: لو حصل أي خطأ في التوليد نرجع للقائمة الثابتة
        dev = random.choice(DEVICES)

    # detect proxy country for locale/timezone matching
    proxy_country = 'default'
    if proxy:
        import re as _re
        m = _re.search(r'__cr\.([a-z,]+)', proxy.lower())
        if m:
            countries = [c for c in m.group(1).split(',') if c in COUNTRY_PROFILES]
            if countries:
                proxy_country = random.choice(countries)
    profile = COUNTRY_PROFILES[proxy_country]
    locale  = random.choice(profile['locales'])
    tz      = profile['tz']

    final_url, ref = _build_url(url, traffic_mix, locale)

    # UA تطبيق فيسبوك الداخلي: آيفون FBAN/FBIOS ، أندرويد FB_IAB/FB4A (الموبايل فقط —
    # الديسكتوب بيفتح فيسبوك ويب بمتصفح عادي، مفيش تطبيق داخلي).
    ua         = dev['ua']
    is_ios     = dev.get('is_ios', False)
    is_desktop = dev.get('is_desktop', False)
    if (not is_desktop) and ref and ('facebook.com' in ref or 'messenger' in ref) and random.random() < 0.8:
        ua = _fb_app_ua(ua, 'messenger' in ref, is_ios=is_ios,
                        ios_ver=dev.get('ios_ver','18.3'),
                        ios_model=dev.get('ios_model','iPhone15,2'),
                        dpr=dev.get('dpr',3.0))

    def _launch_opts(pstr):
        opts = {
            'headless': True,
            'executable_path': CHROMIUM_BIN,
            'args': ['--no-sandbox','--disable-setuid-sandbox',
                     '--disable-blink-features=AutomationControlled',
                     '--disable-dev-shm-usage','--disable-gpu','--no-zygote',
                     '--disable-webrtc',
                     f'--window-size={dev["vw"]},{dev["vh"]}'],
        }
        pc = parse_proxy(pstr)
        if pc:
            opts['proxy'] = pc
        return opts

    hdrs = {
        'Accept-Language': f'{locale},{locale[:2]};q=0.9,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Upgrade-Insecure-Requests': '1',
    }
    if ref:
        hdrs['Referer'] = ref
    # Client Hints متطابقة مع UA — أندرويد كروم بس (#1)
    ch_hdrs, uad = _client_hints(dev)
    hdrs.update(ch_hdrs)
    uad_js = json.dumps(uad) if uad else 'null'

    noise_seed = random.randint(2, 254)
    mem_val    = dev.get('mem')
    _is_safari = dev.get('engine') == 'safari'
    stealth = (STEALTH_JS
               .replace('__UAD__',           uad_js)
               .replace('__IS_IOS__',        'true' if is_ios else 'false')
               .replace('__IS_SAFARI__',     'true' if _is_safari else 'false')
               .replace('__MAXTOUCH__',      '0' if is_desktop else '5')
               .replace('__LOCALE__',        locale)
               .replace('__LOCALE2__',       locale[:2])
               .replace('__PLATFORM__',      dev.get('platform', 'Linux aarch64'))
               .replace('__VENDOR__',        dev.get('vendor', 'Google Inc.'))
               .replace('__ENGINE__',        dev.get('engine', 'chrome'))
               .replace('__CORES__',         str(dev.get('cores', 8)))
               .replace('__MEM__',           str(mem_val) if mem_val is not None else '4')
               .replace('__WEBGL_VENDOR__',  dev.get('webgl_vendor', 'ARM'))
               .replace('__WEBGL_RENDERER__',dev.get('webgl_renderer', 'Mali-G78'))
               .replace('__NOISE_SEED__',    str(noise_seed)))

    browser = None
    t_start = time.time()
    nav_ms  = 0

    async def _open(pstr):
        # حماية تسريب الآي بي: لو البروكسي مطلوب ومفيش بروكسي صالح، ما نفتحش المتصفح خالص
        if require_proxy and not parse_proxy(pstr):
            raise NoProxy('blocked: no valid proxy (IP-leak guard)')
        b   = await playwright.chromium.launch(**_launch_opts(pstr))
        ctx_opts = dict(
            user_agent=ua,
            viewport={'width':dev['vw'],'height':dev['vh']},
            device_scale_factor=dev['dpr'],
            is_mobile=(not is_desktop), has_touch=(not is_desktop),
            locale=locale, timezone_id=tz,
            extra_http_headers=hdrs,
        )
        if reuse_state:                       # زائر عائد: نبدأ من كوكيز الموقع المحفوظة
            ctx_opts['storage_state'] = reuse_state
        ctx = await b.new_context(**ctx_opts)
        await ctx.add_init_script(stealth)
        # تعامل مع الـpopunder: موقع smartlink بيفتح العرض في تاب جديد —
        # نستنى العرض يحمّل ونعمل سكرول خفيف عشان الظهور يتحسب والزيارة تبان حقيقية.
        _popup_state = {'handled': False}
        async def _on_popup(popup):
            # نتعامل مع أول popunder فقط، ونقفل الزيادة (بعض المواقع بترمي كذا تاب = تخنق الرنر)
            if _popup_state['handled']:
                try: await popup.close()
                except Exception: pass
                return
            _popup_state['handled'] = True
            try:
                await _apply_throttle(popup)   # نبطّئ العرض كمان = يقلّل شفط الإعلانات المتجددة
                await popup.wait_for_load_state('domcontentloaded', timeout=12000)
                with _lock:
                    _stats['popunder'] = _stats.get('popunder', 0) + 1
                # === سلوك بشري *متنوّع* تجاه الـpopunder (مش نمط موحّد = أطبع وأصعب في الكشف) ===
                # الظهور اتحسب بالفعل أول ما التاب فتح وحمّل (beacon). الإنسان الحقيقي بيتعامل
                # مع الـpopunder بأشكال مختلفة: حد يقفله بسرعة، حد يتصفّحه، حد يسيبه مفتوح.
                _b = random.random()
                if _b < 0.30:
                    # يتجاهله/يقفله بسرعة (3-9s) — أكتر نوع بشري + أوفر داتا
                    await asyncio.sleep(random.uniform(3.0, 9.0))
                    _keep = False
                elif _b < 0.82:
                    # يتصفّحه شوية (10-30s) بسكرول متقطّع طبيعي، وبعدين يقفله
                    _end = time.time() + random.uniform(10.0, 30.0)
                    while time.time() < _end:
                        await asyncio.sleep(random.uniform(2.5, 7.0))
                        try:
                            await popup.evaluate("window.scrollBy(0, %d)" % random.randint(120, 480))
                        except Exception:
                            break
                    _keep = False
                else:
                    # ~18% يسيبه مفتوح طول الجلسة (زي بعض الناس فعلاً) بسكرول خفيف
                    for _ in range(random.randint(2, 5)):
                        await asyncio.sleep(random.uniform(4.0, 9.0))
                        try:
                            await popup.evaluate("window.scrollBy(0, %d)" % random.randint(120, 480))
                        except Exception:
                            break
                    _keep = True
                if not _keep:
                    try: await popup.close()
                    except Exception: pass
            except Exception:
                pass
        ctx.on('page', lambda p: asyncio.create_task(_on_popup(p)))
        pg = await ctx.new_page()
        await _apply_throttle(pg)          # سرعة موبايل بطيئة (توفير داتا + قعدة أطول)
        # === وضع توفير البيانات: نمنع الصور/الفيديو/الخطوط (بتوفّر ~80% من الداتا) ===
        # الـHTML والجافاسكريبت (التحليلات/بيكسل فيسبوك) بيحمّلوا عادي فالزيارة تتسجّل،
        # والموقع بيشوف متصفح موبايل بوضع موفّر-بيانات (طبيعي تمامًا).
        if DATA_SAVER:
            # توفير أقوى: نحجب الصور + الفيديو + الخطوط في كل مكان.
            # الموقع popunder — الظهور بيتحسب لما التاب يفتح (beacon JS)، مش بالمنظر،
            # والجافاسكريبت (اللي بيطلق الظهور) بيفضل شغّال. توفير ضخم في الداتا.
            async def _saver(route):
                try:
                    if route.request.resource_type in _BLOCK_TYPES:
                        with _lock:
                            _stats['blocked'] = _stats.get('blocked', 0) + 1
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try: await route.continue_()
                    except Exception: pass
            await pg.route('**/*', _saver)
        t_nav = time.time()
        # referer جوه goto بيملّي document.referrer (مش الهيدر بس) — التحليلات تنسب الزيارة لفيسبوك (#1)
        r = await pg.goto(final_url, wait_until='domcontentloaded',
                          timeout=goto_timeout, referer=(ref or None))
        return b, ctx, pg, r, int((time.time() - t_nav) * 1000)

    try:
        with _lock:
            _stats['active'] += 1
            _stats['devices'][dev['name']] = _stats['devices'].get(dev['name'], 0) + 1

        # فتح مع إعادة محاولة على بروكسي تالي — البروكسيات بتغيّر IP كل دقيقة،
        # فالفشل مؤقت: نجرّب واحد تاني من غير ما نشطب أي بروكسي من البول.
        attempts = [proxy]
        if pick_proxy:
            attempts += [pick_proxy() for _ in range(PROXY_RETRIES)]
        page = resp = context = None
        last_err = None
        for pstr in attempts:
            try:
                browser, context, page, resp, nav_ms = await _open(pstr)
                break
            except Exception as e:
                last_err = e
                if browser:
                    try: await browser.close()
                    except: pass
                    browser = None
                continue
        if page is None:
            raise last_err or Exception('proxy open failed')

        if resp:
            code = str(resp.status)
            with _lock:
                _stats['codes'][code] = _stats['codes'].get(code, 0) + 1
                _stats['times'].append(nav_ms)
                if len(_stats['times']) > 500:
                    _stats['times'].pop(0)

        # === وقت الاستيعاب الأولي — إنسان يشوف الصفحة أول ما تفتح ===
        await asyncio.sleep(random.uniform(0.8, 2.2))

        # === حارس الرابط الميت ===
        # بعد ما الصفحة تفتح والـ JS يشتغل، لو الرابط حوّل لصفحة احتياطية
        # (زي adzilla.meme) يبقى الـ Smartlink موقوف — الزيارة مش هتتحسب.
        try:
            landed = _host_of(page.url)
        except Exception:
            landed = ''
        if _is_dead_host(landed):
            raise DeadLink(landed)
        if await _looks_parked(page):
            raise DeadLink(landed or 'parked')

        # === حلقة الأفعال البشرية ===
        # الأوزان: تمرير لأسفل أكثر شيء، ثم ضغط، ثم حركة موس، ثم توقف قراءة، ثم hover، ثم تمرير لأعلى
        # ميل الأفعال حسب الشخصية (#4)
        if persona == 'bouncer':      # يمرّر بسرعة ويطلع، تفاعل أقل
            ACTIONS = (['scroll_dn'] * 5 + ['move'] * 2 + ['pause'] * 1 + ['click'] * 1)
        elif persona == 'reader':     # قراءة متعمّقة: توقفات وتمرير أكثر
            ACTIONS = (['scroll_dn'] * 4 + ['pause'] * 4 + ['click'] * 2 +
                       ['move'] * 2 + ['hover'] * 2 + ['scroll_up'] * 2)
        else:                          # scanner: متوازن
            ACTIONS = (['scroll_dn'] * 4 + ['click'] * 3 + ['move'] * 2 +
                       ['pause'] * 2 + ['hover'] * 1 + ['scroll_up'] * 1)

        try:
            vw = await page.evaluate('window.innerWidth')
            vh = await page.evaluate('window.innerHeight')
        except Exception:
            vw, vh = dev['vw'], dev['vh']

        # ضغط عشوائي على إعلان في جزء صغير من الجلسات
        do_ad_click = random.random() < AD_CLICK_RATE
        ad_clicked  = False
        _next_ad_try = 5.0   # أول محاولة بعد 5 ثوانٍ، وبعدها يعيد كل ~6 ثوانٍ
        nav_recover = 0      # عدّاد استرجاع بعد التنقّل (redirect للعرض)

        while (time.time() - t_start) < duration and not _state['stop']:
            try:
                # الجلسات المختارة (2%) لازم تضغط إعلان أكيد — تعيد المحاولة وتسكرول لحد ما تنجح
                el = time.time() - t_start
                if do_ad_click and not ad_clicked and el > _next_ad_try:
                    ad_clicked = await _try_click_ad(page)
                    if ad_clicked:
                        add_log(f'  → ad♦ {sid:04d} (ضغط إعلان)')
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                        break  # الصفحة ربما تنقّلت — أنهِ الجلسة بأمان
                    else:
                        _next_ad_try = el + 6.0   # مالقاش دلوقتي — يعيد المحاولة بعد شوية (مش يستسلم)

                action = random.choice(ACTIONS)

                # — تمرير لأسفل —
                if action == 'scroll_dn':
                    await _human_scroll(page, random.randint(100, 420))
                    await asyncio.sleep(random.uniform(0.4, 2.0))

                # — تمرير لأعلى —
                elif action == 'scroll_up':
                    await _human_scroll(page, -random.randint(50, 220))
                    await asyncio.sleep(random.uniform(0.3, 1.2))

                # — ضغطة على عنصر مرئي —
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
                            await asyncio.sleep(random.uniform(0.05, 0.18))
                            await page.mouse.up()
                            await asyncio.sleep(random.uniform(0.6, 2.5))
                    except Exception:
                        pass

                # — حركة موس عشوائية (تصفح بدون ضغط) —
                elif action == 'move':
                    pts = random.randint(1, 3)
                    for _ in range(pts):
                        await _human_move(page,
                                          random.uniform(10, vw - 10),
                                          random.uniform(10, vh - 10))
                        await asyncio.sleep(random.uniform(0.15, 0.6))

                # — توقف قراءة —
                elif action == 'pause':
                    await asyncio.sleep(random.uniform(1.2, 4.0))

                # — hover على عنصر —
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

                nav_recover = 0   # الدورة نجحت — صفّر عدّاد استرجاع التنقّل

            except Exception:
                # الصفحة اتنقّلت (redirect للعرض/smartlink)؟ نلحقها ونكمّل التصفّح عليها بدل ما نخرج
                nav_recover += 1
                if nav_recover > 3:
                    break
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=6000)
                    await asyncio.sleep(random.uniform(0.6, 1.5))
                    vw = await page.evaluate('window.innerWidth')
                    vh = await page.evaluate('window.innerHeight')
                    add_log(f'  ↪ {sid:04d} تابع التحويل — بيتصفّح صفحة العرض')
                except Exception:
                    break  # الصفحة فعلاً ماتت

        total_s = int(time.time() - t_start)
        with _lock:
            _stats['ok'] += 1
        # نلتقط كوكيز الموقع لإعادة استخدامها كزائر عائد لاحقاً (#2)
        if context and not reuse_state:
            try: _save_ctx_state(await context.storage_state())
            except Exception: pass
        rv = ' ↩' if reuse_state else ''
        sc = f'[{resp.status}]' if resp else ''
        add_log(f'✓ {sid:04d} {sc} {dev["name"]}  {nav_ms}ms  {total_s}s  {locale} {persona}{rv}')

    except DeadLink as dl:
        with _lock:
            _stats['err'] += 1
            _stats['dead'] = _stats.get('dead', 0) + 1
            hit = _stats['dead']
            if hit >= DEAD_LIMIT:
                _state['stop'] = True
        add_log(f'⚠ {sid:04d} DEAD → {dl} (الرابط ميت/موقوف — مش بيتحسب)')
        if hit >= DEAD_LIMIT:
            add_log(f'⛔ إيقاف تلقائي: {hit} هبوط ميت متتالي — وفّرنا البروكسي')
    except NoProxy:
        with _lock:
            _stats['err'] += 1
            _stats['noproxy'] = _stats.get('noproxy', 0) + 1
        add_log(f'🛡 {sid:04d} مُلغاة — مفيش بروكسي (حماية تسريب الآي بي)')
    except Exception as e:
        with _lock:
            _stats['err'] += 1
        add_log(f'✗ {sid:04d} {type(e).__name__}: {str(e)[:80]}')
    finally:
        with _lock:
            _stats['active'] -= 1
        if browser:
            try: await browser.close()
            except: pass

# ===== Adaptive autoscaler (تحكّم تلقائي في عدد المتصفحات حسب الضغط) =====
# المتصفحات تقوم واحد ورا التاني بالتدريج؛ لو فيه مساحة يزوّد لحد السقف، ولو ضغط يهدّي.
_autoscale = {'target': 0, 'max': 0, 'cpu': 0.0, 'mem': 0}
_cpu_prev  = {'total': 0, 'idle': 0}

def _cpu_percent():
    """نسبة استخدام المعالج من فرق /proc/stat بين نداءين (لينكس)."""
    try:
        with open('/proc/stat') as f:
            v = list(map(int, f.readline().split()[1:]))
        idle  = v[3] + (v[4] if len(v) > 4 else 0)
        total = sum(v)
        dt = total - _cpu_prev['total']
        di = idle  - _cpu_prev['idle']
        _cpu_prev['total'] = total
        _cpu_prev['idle']  = idle
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1 - di / dt)))
    except Exception:
        return 50.0

def _mem_avail_mb():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 1024

def _loadavg1():
    try:
        with open('/proc/loadavg') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0

def _cpu_count():
    try:
        return os.cpu_count() or 4
    except Exception:
        return 4

HARD_CAP = 10   # سقف المتصفحات لكل رنر — الـpopunder دلوقتي بيقفل بدري (82%) فالحِمل أقل؛ الـautoscaler بيهدّي تلقائياً لو ضغط

async def _autoscaler(max_target):
    """يعدّل _autoscale['target'] — الأولوية لبقاء الرنر حيًّا مش عصر آخر متصفح."""
    cores = _cpu_count()
    _autoscale['max'] = min(max_target, HARD_CAP)
    _autoscale['target'] = min(2, max_target)   # يبدأ صغير ويصعد تدريجياً
    _cpu_percent()                              # عيّنة أولى للـ delta
    await asyncio.sleep(2)
    while _state.get('running') and not _state['stop']:
        cpu  = _cpu_percent()
        mem  = _mem_avail_mb()
        load = _loadavg1()
        with _lock:
            done = _stats['ok'] + _stats['err']
            errp = (_stats['err'] / done * 100) if done >= 8 else 0.0
        _autoscale['cpu']  = round(cpu, 0)
        _autoscale['mem']  = mem
        _autoscale['load'] = round(load, 1)
        tgt = _autoscale['target']
        # باك-أوف مبكر للحفاظ على استجابة الرنر (وكيل GitHub لازم يفضل يرد وإلا يعتبره ميت)
        # نراقب الـload مش الـCPU بس — الـload بيمسك ازدحام الطابور وانتظار I/O
        if load > cores * 2.8 or mem < 500:
            new = max(2, tgt // 2)          # فرملة طوارئ مبكرة — الرنر بيختنق
        elif cpu > 82 or load > cores * 2.2 or mem < 700 or errp > 20:
            new = max(2, tgt - 2)
        # مساحة آمنة → زوّد متصفح واحد بحذر
        elif cpu < 68 and load < cores * 1.5 and mem > 1000 and errp < 8:
            new = min(_autoscale['max'], tgt + 1)
        else:
            new = tgt
        if new != tgt:
            _autoscale['target'] = new
            arrow = '▲' if new > tgt else '▼'
            add_log(f'⚙️ {arrow} {new} متصفح  (CPU {cpu:.0f}% · حِمل {load:.1f}/{cores} · RAM {mem}MB · خطأ {errp:.0f}%)')
        await asyncio.sleep(4)

# ===== Master runner =====
async def _master(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix=True, goto_timeout=90000):
    _state['stop'] = False
    reset_stats(count)

    # بول البروكسيات: حقل proxy ممكن يحمل عدة بروكسيات (واحد في كل سطر) → تدوير لكل جلسة
    _pool = [p.strip() for p in (proxy or '').replace('\r', '').split('\n') if p.strip()]
    def _pick(i):
        return _pool[(i - 1) % len(_pool)] if _pool else None
    # مزوّد بروكسي عشوائي لإعادة المحاولة (كل البروكسيات متكافئة وبتغيّر IP كل دقيقة)
    _rand_proxy = (lambda: random.choice(_pool)) if _pool else (lambda: None)
    # حماية تسريب الآي بي: لو في بول بروكسي، كل جلسة لازم يكون ليها بروكسي (وإلا تُلغى)
    require_proxy = len(_pool) > 0

    async with async_playwright() as pw:

        # === فحص مبدئي: جلسة واحدة تتأكد إن الرابط حي قبل ما نصرف بروكسي ===
        # لو هبطت على صفحة ميتة (adzilla) نوقف فوراً من غير ما نشغّل الأسطول.
        add_log('🔎 فحص مبدئي للرابط…')
        # حماية بروكسي: قبل ما نصرف أي حاجة، نتأكد في بروكسي حي فعليًا
        if require_proxy:
            import asyncio as _a
            alive = await _a.get_event_loop().run_in_executor(None, _probe_proxy, _pick(1))
            if not alive:
                alive = await _a.get_event_loop().run_in_executor(None, _probe_proxy, _rand_proxy())
            if not alive:
                add_log('🛑 توقف: البروكسي واقف/منتهي — مفيش بروكسي حي. الحملة اتوقفت لحماية آي بي السيرفر.')
                _pause_campaign('proxy_down')
                _state['running'] = False
                return
        await run_session(pw, url, _pick(1), min(duration, 8), 0, 0,
                          traffic_mix, goto_timeout, pick_proxy=_rand_proxy,
                          require_proxy=require_proxy)
        if _state['stop'] or _stats.get('dead', 0) > 0:
            _state['stop'] = True
            add_log('⛔ توقف: الرابط بيحوّل لصفحة ميتة — لم يُستهلك بروكسي على الفاضي. جدّد الـ Smartlink.')
            _pause_campaign('dead_link')   # عشان الاستئناف التلقائي ما يعيدش رابط ميت
            _state['running'] = False
            return
        add_log('✅ الرابط حي — بدء التشغيل الكامل')

        w0 = _hour_weight()
        add_log(f'⏰ وزن الساعة {time.localtime().tm_hour:02d}:00 = {w0:.2f} '
                f'({"ذروة" if w0>=0.8 else "هدوء" if w0<=0.3 else "عادي"})')

        # السقف الأقصى للمتصفحات المتزامنة (الإعداد = السقف؛ الـautoscaler يوصلّه تدريجياً)
        max_target = max(1, int(concurrency))
        add_log(f'🚀 تشغيل تكيّفي — يقوم متصفح ورا التاني، يزوّد لحد {max_target} لو فيه مساحة، ويهدّي لو ضغط')

        # يشغّل الـautoscaler بالتوازي مع مطلِق الجلسات
        scaler = asyncio.create_task(_autoscaler(max_target))

        # حارس البروكسي: كل 60ث يتأكد في بروكسي حي فعليًا؛ 3 فشل متتالي → يوقف الحملة
        async def _proxy_guard():
            if not _pool:
                return
            loop  = asyncio.get_event_loop()
            fails = 0
            while _state.get('running') and not _state['stop']:
                await asyncio.sleep(60)
                ok = await loop.run_in_executor(None, _probe_proxy, random.choice(_pool))
                if ok:
                    fails = 0
                else:
                    fails += 1
                    add_log(f'⚠️ فحص البروكسي فشل ({fails}/3)')
                    if fails >= 3:
                        _state['stop'] = True
                        add_log('🛑 إيقاف تلقائي: البروكسي واقف/منتهي (3 فحوصات فشلت) — حماية آي بي السيرفر')
                        _pause_campaign('proxy_down')
                        break
        guard = asyncio.create_task(_proxy_guard())

        SPAWN_GAP = 0.6          # فجوة القيام بين متصفح والتالي (قيام تدريجي، مش دفعة)
        inflight  = set()
        launched  = 0
        try:
            while launched < count and not _state['stop']:
                # إيقاف تلقائي لو الأخطاء عدّت الحد
                if err_thresh > 0:
                    with _lock:
                        done = _stats['ok'] + _stats['err']
                        if done >= 10 and _stats['err'] / done * 100 >= err_thresh:
                            _state['stop'] = True
                            add_log(f'⛔ إيقاف تلقائي: تجاوز حد الأخطاء {err_thresh}%')
                            break
                tgt = _autoscale['target'] or 1
                # يطلق جلسات لحد ما العدد الطائر يوصل للهدف الحالي
                while len(inflight) < tgt and launched < count and not _state['stop']:
                    launched += 1
                    t = asyncio.create_task(
                        run_session(pw, url, _pick(launched), duration, launched, 0,
                                    traffic_mix, goto_timeout, pick_proxy=_rand_proxy,
                                    require_proxy=require_proxy))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
                    # تباعد وقت الهدوء (#3) مضروب في فجوة القيام
                    w = _hour_weight()
                    gap = SPAWN_GAP + (random.uniform(0, (1.0 - w) * 4.0) if w < 1.0 else 0)
                    await asyncio.sleep(gap)
                await asyncio.sleep(0.4)
        finally:
            _state['stop'] = True if _state['stop'] else _state['stop']
            if inflight:
                await asyncio.gather(*list(inflight), return_exceptions=True)
            scaler.cancel()
            guard.cancel()

    _state['running'] = False

def _thread(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix=True, goto_timeout=90000):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_master(url, proxy, count, concurrency, duration, jitter, err_thresh, traffic_mix, goto_timeout))
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
                        'msg':'لا يوجد بروكسي — IP السيرفر المباشر'})

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
                        'msg':'⚠️ نفس IP السيرفر! البروكسي لا يعمل' if same_ip else '✓ البروكسي يعمل'})
    except Exception as e:
        return jsonify({'ok':False,'msg':f'فشل: {type(e).__name__}: {str(e)[:120]}',
                        'ms':round((time.time()-t0)*1000)})

@app.route('/diag_goto', methods=['POST'])
def diag_goto():
    """تشخيص: goto حقيقي بـChromium عبر بروكسي معيّن (لاختبار إعدادات بروكسي مختلفة من الرنر)."""
    import asyncio
    d      = request.json or {}
    proxy  = (d.get('proxy') or '').strip()
    url    = (d.get('url') or 'https://ipinfo.io/json').strip()
    async def _run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            opts = {'headless':True,'executable_path':CHROMIUM_BIN,
                    'args':['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
                            '--disable-gpu','--no-zygote']}
            pc = parse_proxy(proxy)
            if pc: opts['proxy'] = pc
            b = await pw.chromium.launch(**opts)
            ctx = await b.new_context()
            pg  = await ctx.new_page()
            t0  = time.time()
            try:
                r   = await pg.goto(url, wait_until='domcontentloaded', timeout=30000)
                out = {'ok':True, 'status': (r.status if r else None),
                       'ms': int((time.time()-t0)*1000)}
            except Exception as e:
                out = {'ok':False, 'err': f'{type(e).__name__}: {str(e)[:90]}',
                       'ms': int((time.time()-t0)*1000)}
            try: await b.close()
            except Exception: pass
            return out
    try:
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as e:
        res = {'ok':False, 'err':'host: '+f'{type(e).__name__}: {str(e)[:90]}'}
    return jsonify(res)

async def _start_connect_relay(upstream_proxy):
    """يشغّل relay محلي (127.0.0.1) بيعمل HTTP CONNECT للـupstream (dataimpulse)
       بمصادقة في بايثون — Chromium بيتصل بيه بدون مصادقة. بيرجّع (server, port)."""
    import base64
    pc = parse_proxy(upstream_proxy)
    up_server = pc['server'].split('://', 1)[1]              # host:port
    up_host, up_port = up_server.rsplit(':', 1); up_port = int(up_port)
    auth_hdr = ''
    if pc.get('username'):
        raw = f"{pc['username']}:{pc.get('password','')}"
        auth_hdr = 'Proxy-Authorization: Basic ' + base64.b64encode(raw.encode()).decode() + '\r\n'

    async def _pipe(r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data); await w.drain()
        except Exception:
            pass
        try: w.close()
        except Exception: pass

    async def handle(creader, cwriter):
        try:
            head = await creader.readuntil(b'\r\n\r\n')
            first = head.split(b'\r\n', 1)[0].decode('latin1')
            parts = first.split(' ')
            if len(parts) < 2 or parts[0].upper() != 'CONNECT':
                cwriter.write(b'HTTP/1.1 405 Method Not Allowed\r\n\r\n'); await cwriter.drain()
                cwriter.close(); return
            target = parts[1]
            ureader, uwriter = await asyncio.open_connection(up_host, up_port)
            uwriter.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth_hdr}\r\n".encode())
            await uwriter.drain()
            resp = await ureader.readuntil(b'\r\n\r\n')
            if b' 200' not in resp.split(b'\r\n', 1)[0]:
                cwriter.write(b'HTTP/1.1 502 Bad Gateway\r\n\r\n'); await cwriter.drain()
                cwriter.close(); return
            cwriter.write(b'HTTP/1.1 200 Connection established\r\n\r\n'); await cwriter.drain()
            await asyncio.gather(_pipe(creader, uwriter), _pipe(ureader, cwriter))
        except Exception:
            try: cwriter.close()
            except Exception: pass

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    return server, port

@app.route('/diag_relay', methods=['POST'])
def diag_relay():
    """تشخيص: Chromium عبر relay محلي بدون مصادقة يوصّل لـupstream dataimpulse.
       بيختبر لو المشكلة في اتصال Chromium المباشر بالبروكسي (client-based block)."""
    import asyncio
    d      = request.json or {}
    proxy  = (d.get('proxy') or '').strip()
    url    = (d.get('url') or 'https://ipinfo.io/json').strip()
    async def _run():
        from playwright.async_api import async_playwright
        server, rport = await _start_connect_relay(proxy)
        try:
            async with async_playwright() as pw:
                opts = {'headless':True,'executable_path':CHROMIUM_BIN,
                        'args':['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
                                '--disable-gpu','--no-zygote'],
                        'proxy':{'server':f'http://127.0.0.1:{rport}'}}
                b = await pw.chromium.launch(**opts)
                ctx = await b.new_context(); pg = await ctx.new_page()
                t0 = time.time()
                try:
                    r = await pg.goto(url, wait_until='domcontentloaded', timeout=30000)
                    out = {'ok':True,'status':(r.status if r else None),'ms':int((time.time()-t0)*1000),'relay_port':rport}
                except Exception as e:
                    out = {'ok':False,'err':f'{type(e).__name__}: {str(e)[:90]}','ms':int((time.time()-t0)*1000)}
                try: await b.close()
                except Exception: pass
                return out
        finally:
            server.close()
            try: await server.wait_closed()
            except Exception: pass
    try:
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as e:
        res = {'ok':False,'err':'host: '+f'{type(e).__name__}: {str(e)[:90]}'}
    return jsonify(res)

def _spawn_campaign(d):
    """يطلّق الحملة في ثريد مستقل — يستخدمها /start والاستئناف التلقائي."""
    _state['stop']    = False
    _state['running'] = True
    threading.Thread(target=_thread, daemon=True, args=(
        d['url'],
        (d.get('proxy') or '').strip() or None,
        int(d.get('count',50)),
        int(d.get('concurrency',3)),
        float(d.get('duration',15)),
        float(d.get('jitter',0)),
        float(d.get('err_thresh',0)),
        bool(d.get('traffic_mix', True)),
        int(d.get('goto_timeout', 90000)),
    )).start()

@app.route('/start', methods=['POST'])
def start():
    if _state['running']:
        return jsonify({'error':'already_running'}), 400
    d   = request.json or {}
    url = d.get('url','').strip()
    if not url:
        return jsonify({'error':'url_required'}), 400
    d['url']    = url
    d['paused'] = False                 # بدء يدوي جديد = فعّل الاستئناف التلقائي
    d.pop('pause_reason', None)
    save_campaign(d)                     # احفظها عشان تكمّل لوحدها بعد أي rerun
    _spawn_campaign(d)
    return jsonify({'ok':True})

@app.route('/stop', methods=['POST'])
def stop():
    _state['stop']    = True
    _state['running'] = False   # فوري — يخلّي الـ UI يستجيب فوراً
    _pause_campaign('manual_stop')   # إيقاف يدوي = ما يرجعش لوحده في الجوب الجاي
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
                    'scale': dict(_autoscale),
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
  <div class="ct">⚙️ إعدادات الاختبار</div>

  <label>رابط الموقع *</label>
  <input id="url" type="url" placeholder="https://example.com" oninput="saveCfg()">

  <label>البروكسي (اختياري)</label>
  <div style="display:flex;gap:7px;align-items:center">
    <input id="proxy" type="text" placeholder="user:pass@host:port  أو  http://..." style="flex:1" oninput="saveCfg()">
    <button class="btn-sm" id="btnTest" onclick="testProxy()">🔍 اختبار</button>
  </div>
  <div id="proxyResult" style="display:none"></div>

  <div class="g4">
    <div><label>عدد الزيارات</label><input id="count" type="number" value="30" min="1" max="5000" oninput="saveCfg()"></div>
    <div><label>تزامن</label><input id="conc" type="number" value="3" min="1" max="15" oninput="saveCfg()"></div>
    <div><label>مدة الجلسة (ث)</label><input id="dur" type="number" value="20" min="5" max="60" oninput="saveCfg()"></div>
    <div><label>جيتر (ث) ⓘ</label><input id="jitter" type="number" value="0" min="0" max="30" step="0.5" title="تأخير عشوائي بين بدء الجلسات" oninput="saveCfg()"></div>
  </div>

  <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
    <label style="margin:0;white-space:nowrap">إيقاف تلقائي إذا وصلت الأخطاء</label>
    <input id="errThresh" type="number" value="0" min="0" max="100" style="width:70px" title="0 = معطّل" oninput="saveCfg()">
    <span style="font-size:12px;color:var(--muted)">% (0 = معطّل)</span>
  </div>

  <div class="g2" style="margin-top:12px">
    <button class="btn btn-go"   id="btnGo"   onclick="doStart()">▶ ابدأ الاختبار</button>
    <button class="btn btn-stop" id="btnStop" onclick="doStop()" disabled>⏹ إيقاف</button>
  </div>
</div>

<!-- Stats -->
<div class="card">
  <div class="hdr">
    <span class="ct" style="margin:0">📊 إحصائيات مباشرة</span>
    <div style="display:flex;align-items:center;gap:8px">
      <span id="okRate" class="rate-pill rate-hi" style="display:none"></span>
      <span class="badge b-idle" id="badge">⏹ متوقف</span>
    </div>
  </div>

  <div class="stats5">
    <div class="stat"><div class="num c-ok"  id="sOk">0</div><div class="lbl">✓ نجاح</div></div>
    <div class="stat"><div class="num c-err" id="sErr">0</div><div class="lbl">✗ أخطاء</div></div>
    <div class="stat"><div class="num c-act" id="sAct">0</div><div class="lbl">🔵 نشط</div></div>
    <div class="stat"><div class="num c-spd" id="sRps">0.0</div><div class="lbl">⚡ جلسة/ث</div></div>
    <div class="stat"><div class="num c-ms"  id="sMs">—</div><div class="lbl">⏱ avg ms</div></div>
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
      <span>جلسة/ثانية — آخر 60s</span>
      <span id="sparkMax" style="color:var(--spd)"></span>
    </div>
    <svg class="sparkline" id="sparkSvg" preserveAspectRatio="none"></svg>
  </div>
</div>

<!-- Status codes -->
<div class="card">
  <div class="ct">🔢 توزيع الاستجابات</div>
  <div class="codes-bar" id="codesBar"><span style="color:var(--muted);font-size:11px;padding:3px 8px">لا بيانات بعد</span></div>
  <div class="codes-legend">
    <div class="leg-item"><div class="leg-dot" style="background:#3fb950"></div>2xx نجاح</div>
    <div class="leg-item"><div class="leg-dot" style="background:#58a6ff"></div>3xx تحويل</div>
    <div class="leg-item"><div class="leg-dot" style="background:#e3b341"></div>4xx خطأ عميل</div>
    <div class="leg-item"><div class="leg-dot" style="background:#f85149"></div>5xx خطأ سيرفر</div>
  </div>
  <div id="codesDetail" style="margin-top:8px;font-size:11px;color:var(--muted);direction:ltr"></div>
</div>

<!-- Devices -->
<div class="card">
  <div class="ct">📱 الأجهزة المستخدمة</div>
  <div class="dev-chips" id="devChips"></div>
</div>

<!-- Log -->
<div class="card">
  <div class="log-hdr">
    <span class="ct" style="margin:0">📋 سجل العمليات</span>
    <div style="display:flex;gap:5px;align-items:center">
      <div class="log-filters">
        <button class="log-btn active" id="fAll"  onclick="setFilter('all')">الكل</button>
        <button class="log-btn"        id="fOk"   onclick="setFilter('ok')">✓</button>
        <button class="log-btn"        id="fErr"  onclick="setFilter('err')">✗</button>
      </div>
      <a href="/export" class="btn-sm" style="text-decoration:none;display:inline-flex;align-items:center;padding:2px 9px;height:25px">⬇ تصدير</a>
    </div>
  </div>
  <div class="log-box" id="logBox"><span style="color:#484f58">جاهز للبدء...</span></div>
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
  btn.disabled=true; btn.textContent='⏳';
  box.style.display='none';
  fetch('/test_proxy',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({proxy:prx})})
  .then(r=>r.json()).then(d=>{
    btn.disabled=false; btn.textContent='🔍 اختبار';
    box.style.display='block';
    if(!d.ok){box.className='px-err';box.textContent=d.msg;return;}
    if(d.mode==='direct'){
      box.className='px-warn';
      box.textContent=`⚠ بدون بروكسي — IP: ${d.ip}  ${d.country}  ${d.org}`;
    } else if(d.same_ip){
      box.className='px-warn';
      box.textContent=`⚠ نفس IP السيرفر!  IP: ${d.ip}  ${d.ms}ms`;
    } else {
      box.className='px-ok';
      box.textContent=`✓ يعمل  IP: ${d.ip}  ${d.country} ${d.city}  ${d.org}  ${d.ms}ms`;
    }
  }).catch(e=>{
    btn.disabled=false; btn.textContent='🔍 اختبار';
    box.className='px-err'; box.style.display='block'; box.textContent='خطأ: '+e;
  });
}

// ===== start / stop =====
function doStart(){
  const url=$('url').value.trim();
  if(!url){toast('أدخل رابط الموقع','err');return;}
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
    toast('بدأ الاختبار','info');
  });
}
function doStop(){
  fetch('/stop',{method:'POST'});
  $('btnStop').disabled=true;
  toast('جاري الإيقاف...','info');
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
  const filtered=log.filter(l=>_logFilter==='all'||(l.startsWith('✓')&&_logFilter==='ok')||(l.startsWith('✗')&&_logFilter==='err'));
  $('logBox').innerHTML=[...filtered].reverse().map(l=>`<div class="${l.startsWith('✓')?'log-ok':'log-err'}">${l}</div>`).join('') || '<span style="color:#484f58">لا توجد نتائج</span>';
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
  bar.innerHTML=segs||'<span style="color:var(--muted);font-size:11px;padding:3px 8px">لا بيانات</span>';
  $('codesDetail').textContent=Object.entries(codes).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${k}: ${v}`).join('  ');
}

// ===== devices =====
function updateDevices(devs){
  const box=$('devChips');
  if(!Object.keys(devs).length){
    box.innerHTML='<span style="color:var(--muted);font-size:12px">لا بيانات بعد</span>';
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
    $('sMs').textContent=d.avg_ms?d.avg_ms+'ms':'—';

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
      badge.className='badge b-run'; badge.textContent='🟢 يعمل';
    } else if(done>0 && done===d.total){
      badge.className='badge b-done'; badge.textContent='✓ اكتمل';
    } else {
      badge.className='badge b-idle'; badge.textContent='⏹ متوقف';
    }

    if(!d.running && prevRunning){
      $('btnGo').disabled=false; $('btnStop').disabled=true;
      toast(`انتهى! ✓${d.ok} ✗${d.err}`, d.err===0?'ok':'info');
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

def _autostart():
    """يكمّل آخر حملة أوتوماتيك بعد أي rerun للرنر — من غير ما تفتح الـ UI."""
    time.sleep(10)   # مهلة عشان الشبكة/playwright يجهزوا
    c = load_campaign()
    if not c:
        add_log('ℹ️ مفيش حملة محفوظة — في انتظار البدء من الـ UI')
        return
    if c.get('paused'):
        add_log(f'⏸️ حملة محفوظة لكن متوقفة ({c.get("pause_reason","")}) — محتاجة رابط جديد من الـ UI')
        return
    if _state['running']:
        return
    add_log('♻️ استئناف تلقائي لآخر حملة…')
    _spawn_campaign(c)

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5555
    print(f"KW Mobile Tester v2 → http://0.0.0.0:{port}")
    threading.Thread(target=_autostart, daemon=True).start()
    app.run(host='0.0.0.0', port=port, threaded=True)
