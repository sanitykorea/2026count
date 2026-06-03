#!/usr/bin/env python3
"""
녹색당 개표 현황판 서버
실행: python3 server.py
접속: http://localhost:5000
"""

from flask import Flask, jsonify, request, send_from_directory
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

app = Flask(__name__, static_folder=os.path.dirname(os.path.abspath(__file__)))
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 사진 업로드 최대 20 MB

# ── gboard 캐시 (Render 30s 타임아웃 방지) ───────────────────
_gboard_cache = {'data': None, 'ts': 0}
_gboard_cache_lock = threading.Lock()
GBOARD_CACHE_TTL = 25  # seconds

# ── 채팅 ─────────────────────────────────────────────────────
_chat_messages = []
_chat_lock = threading.Lock()
_chat_counter = 0

# ── 채팅 모더레이션 ───────────────────────────────────────────
_pinned_msg_id = None
_blocked_ips   = set()
_mod_lock = threading.Lock()


def get_client_ip():
    """프록시(Render, nginx 등) 뒤에서도 실제 클라이언트 IP를 반환."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'

# ── 서버사이드 공유 상태 ──────────────────────────────────────
_photos = {}              # {0: 'data:image/...', 1: ..., 2: ...}
_photos_lock = threading.Lock()

_display_override = None  # None → NEC 크롤링 / list → 관리자 수동 오버라이드
_override_lock = threading.Lock()

# ── 관리자 비밀번호 (환경변수 ADMIN_PASSWORD 로 설정, 기본 green2026) ──
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'green2026')

# ── CORS (브라우저 fetch 허용) ───────────────────────────────
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# ── 상수 ─────────────────────────────────────────────────────
ELECTION_ID = '0020260603'   # 제9회 전국동시지방선거 2026.6.3
BASE_URL    = 'https://info.nec.go.kr'

ELECTION_TYPES = {
    '3': '시·도지사선거', '4': '구·시·군의장선거',
    '5': '시·도의회의원선거', '6': '구·시·군의회의원선거',
    '8': '광역의원비례대표', '9': '기초의원비례대표', '11': '교육감선거',
}

CITIES = {
    '0':'전체','1100':'서울특별시','2600':'부산광역시','2700':'대구광역시',
    '2800':'인천광역시','2900':'광주광역시','3000':'대전광역시','3100':'울산광역시',
    '5100':'세종특별자치시','4100':'경기도','5200':'강원특별자치도',
    '4300':'충청북도','4400':'충청남도','5300':'전북특별자치도','4600':'전라남도',
    '4700':'경상북도','4800':'경상남도','4900':'제주특별자치도',
}

# ── GBoard 3개 선거구 — 브라우저 DevTools 실측 POST 파라미터 ──
# posts: 각 선거구별 POST 파라미터 목록 (제주는 제주시+서귀포시 2개)
GBOARD_DISTRICTS = [
    {
        'id': 0,  # 경상북도 안동시 마선거구
        'is_pr': False,
        'posts': [
            {'electionCode':'6', 'cityCode':'4700', 'sggCityCode':'-1',
             'townCodeFromSgg':'-1', 'townCode':'4706', 'sggTownCode':'6470605',
             'checkCityCode':'-1', 'x':'68', 'y':'7'},
        ],
    },
    {
        'id': 1,  # 제주특별자치도 광역비례 (제주시+서귀포시 합산)
        'is_pr': True,
        'posts': [
            {'electionCode':'8', 'cityCode':'4900', 'sggCityCode':'-1',
             'townCodeFromSgg':'-1', 'townCode':'4901', 'sggTownCode':'-1',
             'checkCityCode':'-1', 'x':'84', 'y':'21'},
            {'electionCode':'8', 'cityCode':'4900', 'sggCityCode':'-1',
             'townCodeFromSgg':'-1', 'townCode':'4902', 'sggTownCode':'-1',
             'checkCityCode':'-1', 'x':'57', 'y':'34'},
        ],
    },
    {
        'id': 2,  # 서울특별시 강서구 라선거구
        'is_pr': False,
        'posts': [
            {'electionCode':'6', 'cityCode':'1100', 'sggCityCode':'-1',
             'townCodeFromSgg':'-1', 'townCode':'1116', 'sggTownCode':'6111604',
             'checkCityCode':'-1', 'x':'70', 'y':'25'},
        ],
    },
]

# ── 후보자 명부 조회 (CPRI03) ────────────────────────────────
# 각 선거구별 CPRI03 파라미터
CANDIDATE_PARAMS = [
    {  # 0: 경상북도 안동시 마선거구 (기초의회의원)
        'electionCode': '6', 'cityCode': '4700', 'sggCityCode': '-1',
        'townCode': '4706', 'sggTownCode': '6470605',
        'proportionalRepresentationCode': '-1',
    },
    {  # 1: 제주 광역의원비례대표
        'electionCode': '8', 'cityCode': '4900', 'sggCityCode': '-1',
        'townCode': '-1', 'sggTownCode': '0',
        'proportionalRepresentationCode': '-1',
    },
    {  # 2: 서울 강서구 라선거구 (기초의회의원)
        'electionCode': '6', 'cityCode': '1100', 'sggCityCode': '-1',
        'townCode': '1116', 'sggTownCode': '6111604',
        'proportionalRepresentationCode': '-1',
    },
]

def fetch_candidates(params):
    """CPRI03 후보자 명부 조회. 후보자 목록 반환."""
    sess = _nec_session()
    show_url = f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=CP&secondMenuId=CPRI03'
    try:
        sess.get(BASE_URL + '/', timeout=6)
        sess.get(show_url, timeout=8)
        sess.headers.update({'Referer': f'{BASE_URL}/electioninfo/electionInfo_report.xhtml'})
    except Exception:
        pass

    post_data = {
        'electionId':                     ELECTION_ID,
        'requestURI':                     f'/electioninfo/{ELECTION_ID}/cp/cpri03.jsp',
        'topMenuId':                      'CP',
        'secondMenuId':                   'CPRI03',
        'menuId':                         'CPRI03',
        'statementId':                    'CPRI03_#00',
        'pageIndex':                      '1',
        'firstIndex':                     '0',
        'recordCountPerPage':             '100',
        'dateCode':                       '0',
        'x':                              '60',
        'y':                              '10',
    }
    post_data.update(params)

    r = sess.post(
        f'{BASE_URL}/electioninfo/electionInfo_report.xhtml',
        data=post_data, timeout=12
    )
    r.raise_for_status()
    return _parse_candidates(r.text)


def _parse_candidates(html):
    """후보자 명부 HTML에서 이름·정당 추출."""
    import re as _re
    soup = BeautifulSoup(html, 'html.parser')

    if any(soup.find(string=lambda t: t and s in t)
           for s in ['조회 자료가 없습니다', '비정상적인 접근', '검색된 결과가 없습니다']):
        return []

    table = soup.find('table', class_='table01') or soup.find('table')
    if not table:
        return []

    # 컬럼 헤더에서 성명·정당 인덱스 파악
    rows = table.find_all('tr')
    name_col = party_col = num_col = None
    for row in rows:
        headers = [th.get_text(strip=True) for th in row.find_all('th')]
        for i, h in enumerate(headers):
            if '성명' in h and name_col is None:   name_col  = i
            if '정당명' in h and party_col is None: party_col = i
            if '기호' in h and num_col is None:    num_col   = i
        if name_col is not None:
            break

    # fallback 인덱스 (기호|성명|한자|생년월일|정당명 순 일반 구조)
    if name_col is None:  name_col  = 2
    if party_col is None: party_col = 5
    if num_col is None:   num_col   = 1

    candidates = []
    GREEN_PARTIES = ['녹색당']
    for row in rows:
        cells = row.find_all('td')
        if not cells or len(cells) < max(name_col, party_col) + 1:
            continue
        name  = cells[name_col].get_text(strip=True)
        party = cells[party_col].get_text(strip=True) if party_col < len(cells) else ''
        if not name or not party:
            continue
        candidates.append({
            'name':    name,
            'party':   party,
            'isGreen': any(g in party for g in GREEN_PARTIES),
        })
    return candidates


@app.route('/api/candidates')
def api_candidates():
    """3개 선거구 실제 후보자 명부 반환."""
    result = []
    for i, params in enumerate(CANDIDATE_PARAMS):
        try:
            cands = fetch_candidates(params)
            result.append({'id': i, 'ok': True, 'candidates': cands})
        except Exception as e:
            result.append({'id': i, 'ok': False, 'candidates': [], 'error': str(e)})
    return jsonify(result)


# ── VCCP08 특정 선거구 조회 (브라우저 실측 파라미터) ─────────
def fetch_district_data(post_params):
    """VCCP08 개표단위별 개표결과로 특정 선거구 데이터 조회."""
    sess = _nec_session()
    # showDocument로 세션 쿠키 획득
    show_url = f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=VC&secondMenuId=VCCP08'
    try:
        sess.get(BASE_URL + '/', timeout=6)
        sess.get(show_url, timeout=8)
    except Exception:
        pass
    # 브라우저 실측: 두 번째 POST의 Referer는 electionInfo_report.xhtml
    sess.headers.update({'Referer': f'{BASE_URL}/electioninfo/electionInfo_report.xhtml'})

    data = {
        'electionId':   ELECTION_ID,
        'requestURI':   f'/electioninfo/{ELECTION_ID}/vc/vccp08.jsp',
        'topMenuId':    'VC',
        'secondMenuId': 'VCCP08',
        'menuId':       'VCCP08',
        'statementId':  'VCCP08_#00',
    }
    data.update(post_params)

    r = sess.post(
        f'{BASE_URL}/electioninfo/electionInfo_report.xhtml',
        data=data, timeout=12
    )
    r.raise_for_status()
    return parse_html(r.text, post_params['electionCode'], post_params['cityCode'])


def _merge_district_results(results):
    """여러 fetch 결과를 후보자별 득표수 합산으로 병합."""
    ok_results = [r for r in results if r.get('status') == 'ok' and r.get('districts')]
    if not ok_results:
        return None

    # 후보자 매핑 (이름 기준)
    cand_map = {}
    total_voter  = 0
    total_votes  = 0

    for res in ok_results:
        for dist in res['districts']:
            total_voter += dist['voter_count']
            total_votes += dist['total_votes']
            for c in dist['candidates']:
                key = c['name']
                if key not in cand_map:
                    cand_map[key] = {'name': c['name'], 'party': c['party'], 'votes': 0}
                cand_map[key]['votes'] += c['votes']

    if not cand_map:
        return None

    candidates = sorted(cand_map.values(), key=lambda c: -c['votes'])
    tot_cand   = sum(c['votes'] for c in candidates)
    for c in candidates:
        c['pct']     = round(c['votes'] / tot_cand * 100, 2) if tot_cand > 0 else 0.0
        c['isGreen'] = '녹색당' in (c.get('party') or '')

    rate   = round(min(100, total_votes / total_voter * 100), 1) if total_voter > 0 and total_votes > 0 else 0
    status = '개표완료' if rate >= 99.9 else ('개표중' if total_votes > 0 else '집계전')
    return {'rate': rate, 'status': status, 'candidates': candidates}


# ── NEC 데이터 수집 (일반 API용 레거시) ─────────────────────
def fetch_nec_data(election_code, city_code):
    sess = _nec_session()
    try:
        sess.get(BASE_URL + '/', timeout=6)
    except Exception:
        pass
    show_url = f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=VC&secondMenuId=VCCP09'
    try:
        sess.get(show_url, timeout=8)
        sess.headers.update({'Referer': show_url})
    except Exception:
        pass

    post_data = {
        'electionId':   ELECTION_ID,
        'requestURI':   f'/electioninfo/{ELECTION_ID}/vc/vccp09.jsp',
        'topMenuId':    'VC',
        'secondMenuId': 'VCCP09',
        'menuId':       'VCCP09',
        'statementId':  f'{ELECTION_ID}.VCCP09_#{election_code}_0',
        'electionCode': str(election_code),
        'cityCode':     str(city_code),
        'sggCityCode':  '0',
        'townCode':     '-1',
        'sggTownCode':  '0',
    }
    r = sess.post(f'{BASE_URL}/electioninfo/electionInfo_report.xhtml', data=post_data, timeout=12)
    r.raise_for_status()
    return parse_html(r.text, election_code, city_code)


def parse_html(html, election_code, city_code):
    import re as _re
    soup = BeautifulSoup(html, 'html.parser')
    result = {
        'election_type': ELECTION_TYPES.get(str(election_code), ''),
        'city':          CITIES.get(str(city_code), str(city_code)),
        'timestamp':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'candidates':    [],
        'districts':     [],
        'status':        'ok',
    }

    no_data_strings = ['조회 자료가 없습니다', '집계된 자료가 없습니다', '해당 자료가 없습니다']
    if any(soup.find(string=lambda t: t and s in t) for s in no_data_strings):
        result['status'] = 'no_data'
        result['message'] = '개표 자료가 없습니다.'
        return result

    table = soup.find('table', class_='table01') or soup.find('table')
    if not table:
        result['status'] = 'no_data'
        result['message'] = '테이블 없음'
        return result

    rows = table.find_all('tr')

    def to_int(s):
        try: return int(_re.sub(r'[^\d]', '', str(s)))
        except: return 0
    def to_float(s):
        try: return float(str(s).strip())
        except: return 0.0
    def is_only_numeric(s):
        return bool(_re.match(r'^[\d,\.]*$', s.strip()))

    # ── 후보자 헤더 파싱 (개선) ──────────────────────────────
    SKIP = {'구시군명','선거인수','투표수','무효투표수','무효','기권수','기권','계','소계',
            '득표율','후보자','후보자명','선거구명','정당명','합계','읍면동명','정당·후보자'}
    candidates_found = []
    for row in rows:
        for th in row.find_all('th'):
            lines = [l.strip() for l in th.get_text(separator='\n').split('\n') if l.strip()]
            if len(lines) < 2:
                continue
            # 첫 줄·둘째 줄 모두 skip 단어 없어야 후보자 헤더로 인식
            if any(w in lines[0] for w in SKIP) or any(w in lines[1] for w in SKIP):
                continue
            entry = {'party': lines[0], 'name': lines[1]}
            if entry not in candidates_found:
                candidates_found.append(entry)
    result['candidates'] = candidates_found

    # ── 데이터 행 분류 ────────────────────────────────────────
    # 첫 번째 td가 비어있거나 숫자만 있으면 득표율 행, 아니면 득표수 행
    tagged = []
    for row in rows:
        tds = row.find_all('td')
        if not tds:
            continue
        first = tds[0].get_text(strip=True)
        tagged.append({'cells': tds, 'first': first,
                       'is_pct_row': not first or is_only_numeric(first)})

    # ── 득표수·득표율 행 쌍 파싱 ─────────────────────────────
    i = 0
    while i < len(tagged):
        row_info = tagged[i]
        if row_info['is_pct_row']:
            i += 1
            continue

        cells = row_info['cells']
        name  = row_info['first']
        # 이름이 비어있거나 순수 숫자면 스킵
        if not name or is_only_numeric(name):
            i += 1
            continue

        raw = [c.get_text(strip=True) for c in cells[1:]]

        # 다음 행이 득표율 행인지 확인
        pct_raw = []
        if i + 1 < len(tagged) and tagged[i + 1]['is_pct_row']:
            pct_cells = tagged[i + 1]['cells']
            pct_vals  = [c.get_text(strip=True) for c in pct_cells]
            # 첫 셀이 비어있으면 offset 제거
            pct_raw = pct_vals[1:] if (pct_vals and not pct_vals[0]) else pct_vals
            i += 2
        else:
            i += 1

        voter = to_int(raw[0]) if len(raw) > 0 else 0
        total = to_int(raw[1]) if len(raw) > 1 else 0

        cands = []
        for j, cand in enumerate(candidates_found):
            idx   = j + 2
            votes = to_int(raw[idx]) if idx < len(raw) else 0
            # 득표율: pct_raw 우선, 없으면 직접 계산
            if j < len(pct_raw) and pct_raw[j]:
                pct = to_float(pct_raw[j])
                if pct == 0.0 and total > 0 and votes > 0:
                    pct = round(votes / total * 100, 2)
            elif total > 0 and votes > 0:
                pct = round(votes / total * 100, 2)
            else:
                pct = 0.0
            cands.append({'party': cand['party'], 'name': cand['name'],
                          'votes': votes, 'pct': pct})
        result['districts'].append({'name': name, 'voter_count': voter,
                                    'total_votes': total, 'candidates': cands})
    return result


# ── GBoard 호환 API (/api/gboard) ───────────────────────────
@app.route('/api/gboard')
def api_gboard():
    """
    GBoard crawl 모드용 JSON 반환.
    형식: [{ "id": 0, "countingRate": 64.5, "status": "개표중", "candidates": [...] }, ...]
    관리자 오버라이드가 설정된 경우 해당 데이터를 우선 반환.
    """
    with _override_lock:
        if _display_override is not None:
            return jsonify(_display_override)

    # 캐시 확인
    with _gboard_cache_lock:
        if _gboard_cache['data'] and (time.time() - _gboard_cache['ts']) < GBOARD_CACHE_TTL:
            return jsonify(_gboard_cache['data'])

    def fetch_one(dc):
        try:
            # 각 POST 파라미터로 조회 (제주는 2개)
            results = []
            for post_params in dc['posts']:
                res = fetch_district_data(post_params)
                results.append(res)

            merged = _merge_district_results(results)
            if not merged:
                return {'id': dc['id'], 'countingRate': 0, 'status': '집계전', 'candidates': []}

            return {
                'id':           dc['id'],
                'countingRate': merged['rate'],
                'status':       merged['status'],
                'candidates':   merged['candidates'],
            }
        except Exception as e:
            print(f'[gboard] district {dc["id"]} error: {e}')
            return {'id': dc['id'], 'countingRate': 0, 'status': '집계전', 'candidates': []}

    # 3개 선거구 병렬 요청
    result_map = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_one, dc): dc['id'] for dc in GBOARD_DISTRICTS}
        for fut in as_completed(futures, timeout=20):
            result_map[futures[fut]] = fut.result()

    result = [result_map[dc['id']] for dc in GBOARD_DISTRICTS]

    with _gboard_cache_lock:
        _gboard_cache['data'] = result
        _gboard_cache['ts']   = time.time()

    return jsonify(result)


# ── 일반 API ─────────────────────────────────────────────────
@app.route('/api/results')
def api_results():
    election_code = request.args.get('election_code', '9')
    city_code     = request.args.get('city_code', '0')
    try:
        data = fetch_nec_data(election_code, city_code)
    except Exception as e:
        data = {'status': 'error', 'message': str(e), 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    return jsonify(data)

@app.route('/api/auth', methods=['POST', 'OPTIONS'])
def api_auth():
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True) or {}
    ok = (data.get('password', '') == ADMIN_PASSWORD)
    return jsonify({'ok': ok})

import re as _re

def _nec_session():
    sess = requests.Session()
    sess.headers.update({
        'User-Agent':                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language':           'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding':           'gzip, deflate, br',
        'Origin':                    BASE_URL,
        'Referer':                   BASE_URL + '/',
        'Cache-Control':             'max-age=0',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest':            'document',
        'Sec-Fetch-Mode':            'navigate',
        'Sec-Fetch-Site':            'same-origin',
        'Sec-Fetch-User':            '?1',
        'sec-ch-ua':                 '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile':          '?0',
        'sec-ch-ua-platform':        '"macOS"',
    })
    return sess

def _parse_turnout_from_soup(soup):
    """투표진행상황 HTML에서 전국 투표율(%) 추출. (rate, asOf) 반환."""
    # 표의 마지막 컬럼이 투표율(%) — "합계" 행을 우선 사용
    table = soup.find('table', class_='table01') or soup.find('table')
    if not table:
        return None, None

    rate = None
    for row in table.find_all('tr'):
        cells = row.find_all('td')
        if not cells:
            continue
        first = cells[0].get_text(strip=True)
        # 합계 행 우선, 없으면 첫 데이터 행
        is_total = '합계' in first
        # 마지막 셀에서 "숫자%" 패턴 추출
        last_cell = cells[-1].get_text(strip=True)
        m = _re.search(r'(\d{1,3}\.\d+)\s*%?', last_cell)
        if m:
            v = float(m.group(1))
            if 0.0 < v <= 100.0:
                rate = v
                if is_total:
                    break  # 합계 행이면 바로 종료

    # 기준시각: 현재 서버 시각 기준
    asOf = datetime.now().strftime('%H:%M') + ' 기준'
    return rate, asOf


def fetch_turnout_data(debug=False):
    """NEC 투표진행상황 페이지에서 전국 투표율을 가져온다."""
    sess = _nec_session()

    # 세션 쿠키 획득: 메인 → showDocument 순으로 방문해 브라우저 흐름 재현
    try:
        sess.get(BASE_URL + '/', timeout=6)
    except Exception:
        pass
    show_url = f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=VC&secondMenuId=VCVP01'
    try:
        sess.get(show_url, timeout=8)
        sess.headers.update({'Referer': show_url})
    except Exception:
        pass

    post_data = {
        'electionId':   ELECTION_ID,
        'requestURI':   f'/electioninfo/{ELECTION_ID}/vc/vcvp01.jsp',
        'topMenuId':    'VC',
        'secondMenuId': 'VCVP01',
        'menuId':       'VCVP01',
        'statementId':  'VCVP01_#2_SUM',   # 브라우저 실측값 (선거ID 접두사 없음)
        'sggTime':      '30시',
        'cityCode':     '0',
        'timeCode':     '30',
        'x':            '53',
        'y':            '19',
    }

    last_html = ''
    last_error = ''

    try:
        r = sess.post(
            f'{BASE_URL}/electioninfo/electionInfo_report.xhtml',
            data=post_data, timeout=12
        )
        r.raise_for_status()
        last_html = r.text

        no_data_msgs = ['조회 자료가 없습니다', '집계된 자료가 없습니다', '비정상적인 접근']
        if any(msg in r.text for msg in no_data_msgs):
            last_error = '투표 집계 전 또는 서비스 준비 중'

            soup = BeautifulSoup(r.text, 'html.parser')
            rate, asOf = _parse_turnout_from_soup(soup)

            if rate is not None:
                result = {
                    'status':    'ok',
                    'rate':      rate,
                    'asOf':      asOf,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                if debug:
                    result['_html'] = r.text[:8000]
                return result

            last_error = 'rate_not_found'

    except Exception as e:
        last_error = str(e)

    result = {
        'status':    'no_data',
        'rate':      None,
        'asOf':      None,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'message':   last_error,
    }
    if debug:
        result['_html'] = last_html[:8000]
    return result


@app.route('/api/turnout')
def api_turnout():
    try:
        data = fetch_turnout_data()
    except Exception as e:
        data = {'status': 'error', 'message': str(e),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'rate': None, 'asOf': None}
    return jsonify(data)


@app.route('/api/debug/turnout')
def api_debug_turnout():
    """관리자용 투표율 디버그. ?password=... 필수."""
    if request.args.get('password') != ADMIN_PASSWORD:
        return jsonify({'ok': False}), 403
    try:
        data = fetch_turnout_data(debug=True)
    except Exception as e:
        data = {'status': 'error', 'message': str(e)}
    return jsonify(data)


def _safe_msg(m):
    """_ip 등 내부 필드를 제거한 클라이언트용 메시지 dict."""
    return {k: v for k, v in m.items() if not k.startswith('_')}

@app.route('/api/chat', methods=['GET'])
def api_chat_get():
    since = int(request.args.get('since', 0))
    with _chat_lock:
        msgs   = [_safe_msg(m) for m in _chat_messages if m['id'] > since and not m.get('deleted')]
        pinned = None
        with _mod_lock:
            if _pinned_msg_id:
                raw = next((m for m in _chat_messages
                            if m['id'] == _pinned_msg_id and not m.get('deleted')), None)
                pinned = _safe_msg(raw) if raw else None
    return jsonify({'messages': msgs, 'pinned': pinned})

@app.route('/api/chat', methods=['POST', 'OPTIONS'])
def api_chat_post():
    if request.method == 'OPTIONS':
        return '', 204
    global _chat_counter
    data = request.get_json(silent=True) or {}
    nick = (data.get('nick') or '익명').strip()[:20] or '익명'
    text = (data.get('text') or '').strip()[:300]
    if not text:
        return jsonify({'ok': False, 'error': '내용 없음'}), 400
    client_ip = get_client_ip()
    with _mod_lock:
        if client_ip in _blocked_ips:
            return jsonify({'ok': False, 'error': '차단된 사용자'}), 403
    is_admin = (data.get('password', '') == ADMIN_PASSWORD)
    with _chat_lock:
        _chat_counter += 1
        msg = {
            'id':      _chat_counter,
            'nick':    nick,
            'text':    text,
            'time':    datetime.now().strftime('%H:%M'),
            'isAdmin': is_admin,
            '_ip':     client_ip,  # 서버 내부 전용, GET 응답에는 미포함
        }
        _chat_messages.append(msg)
        if len(_chat_messages) > 300:
            _chat_messages.pop(0)
    return jsonify({'ok': True, 'id': _chat_counter})


def _require_admin(data):
    return (data or {}).get('password', '') == ADMIN_PASSWORD


@app.route('/api/chat/<int:msg_id>', methods=['DELETE', 'OPTIONS'])
def api_chat_delete(msg_id):
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True) or {}
    if not _require_admin(data):
        return jsonify({'ok': False}), 403
    with _chat_lock:
        for m in _chat_messages:
            if m['id'] == msg_id:
                m['deleted'] = True
                break
    return jsonify({'ok': True})


@app.route('/api/chat/pin', methods=['POST', 'OPTIONS'])
def api_chat_pin():
    if request.method == 'OPTIONS':
        return '', 204
    global _pinned_msg_id
    data = request.get_json(silent=True) or {}
    if not _require_admin(data):
        return jsonify({'ok': False}), 403
    with _mod_lock:
        _pinned_msg_id = data.get('id')  # None이면 핀 해제
    return jsonify({'ok': True})


@app.route('/api/chat/block', methods=['POST', 'OPTIONS'])
def api_chat_block():
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True) or {}
    if not _require_admin(data):
        return jsonify({'ok': False}), 403
    action = data.get('action', 'block')  # 'block' | 'unblock'

    target_ip = None
    if 'msgId' in data:
        # 메시지 ID로 IP 조회
        with _chat_lock:
            msg = next((m for m in _chat_messages if m['id'] == int(data['msgId'])), None)
        if not msg:
            return jsonify({'ok': False, 'error': '메시지 없음'}), 404
        target_ip = msg.get('_ip')
    elif 'ip' in data:
        target_ip = data['ip'].strip()

    if not target_ip:
        return jsonify({'ok': False, 'error': 'IP를 특정할 수 없음'}), 400

    with _mod_lock:
        if action == 'block':
            _blocked_ips.add(target_ip)
        else:
            _blocked_ips.discard(target_ip)
    return jsonify({'ok': True, 'ip': target_ip, 'action': action,
                    'blocked_count': len(_blocked_ips)})


@app.route('/api/chat/blocked', methods=['GET'])
def api_chat_blocked():
    pw = request.args.get('password', '')
    if pw != ADMIN_PASSWORD:
        return jsonify({'ok': False}), 403
    with _mod_lock:
        return jsonify({'blocked_ips': sorted(_blocked_ips)})


# ── 사진 (서버사이드 공유) ────────────────────────────────────
@app.route('/api/photos', methods=['GET'])
def api_photos_get():
    with _photos_lock:
        return jsonify(_photos)


@app.route('/api/photos/<int:photo_id>', methods=['POST', 'DELETE', 'OPTIONS'])
def api_photo_set(photo_id):
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True) or {}
    if not _require_admin(data):
        return jsonify({'ok': False}), 403
    with _photos_lock:
        if request.method == 'DELETE':
            _photos.pop(photo_id, None)
        else:
            _photos[photo_id] = data.get('data', '')
    return jsonify({'ok': True})


# ── 디스플레이 오버라이드 (관리자 수동 데이터 전체 브로드캐스트) ──
@app.route('/api/override', methods=['GET'])
def api_override_get():
    with _override_lock:
        if _display_override is None:
            return jsonify({'active': False})
        return jsonify({'active': True})


@app.route('/api/override', methods=['POST', 'DELETE', 'OPTIONS'])
def api_override_set():
    if request.method == 'OPTIONS':
        return '', 204
    global _display_override
    data = request.get_json(silent=True) or {}
    if not _require_admin(data):
        return jsonify({'ok': False}), 403
    if request.method == 'DELETE':
        with _override_lock:
            _display_override = None
        return jsonify({'ok': True})
    payload = data.get('payload')
    if not payload:
        return jsonify({'ok': False, 'error': 'payload 없음'}), 400
    with _override_lock:
        _display_override = payload
    return jsonify({'ok': True})

@app.route('/')
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')


if __name__ == '__main__':
    print('=' * 55)
    print('  🟢 녹색당 개표 현황판 서버 시작!')
    print('  📊 http://localhost:5000 에서 확인하세요')
    print('  ⏹  종료: Ctrl+C')
    print('=' * 55)
    port = int(os.environ.get('PORT', 8787))
    app.run(port=port, host='0.0.0.0', debug=False)
