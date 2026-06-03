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
import os

app = Flask(__name__, static_folder=os.path.dirname(os.path.abspath(__file__)))

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

# ── GBoard 3개 선거구 설정 ───────────────────────────────────
# id는 HTML DISTRICTS 배열의 인덱스(0·1·2)와 일치해야 함
GBOARD_DISTRICTS = [
    {
        'id':            0,
        'election_code': 6,       # 구·시·군의회의원선거
        'city_code':     '4700',  # 경상북도
        'row_filter':    '안동',  # 행 이름에 이 문자열이 포함된 행 사용
        'sub_filter':    '마',    # 추가 필터 (선거구 이름)
        'is_pr':         False,
    },
    {
        'id':            1,
        'election_code': 8,       # 광역의원비례대표
        'city_code':     '4900',  # 제주특별자치도
        'row_filter':    None,    # None이면 합계 행 사용
        'sub_filter':    None,
        'is_pr':         True,
    },
    {
        'id':            2,
        'election_code': 6,       # 구·시·군의회의원선거
        'city_code':     '1100',  # 서울특별시
        'row_filter':    '강서구',
        'sub_filter':    '라',
        'is_pr':         False,
    },
]

# ── NEC 데이터 수집 ──────────────────────────────────────────
def fetch_nec_data(election_code, city_code):
    sess = requests.Session()
    sess.headers.update({
        'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Referer':         BASE_URL + '/',
        'Origin':          BASE_URL,
    })
    try:
        sess.get(
            f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=VC&secondMenuId=VCCP09',
            timeout=15
        )
    except Exception:
        pass

    post_data = {
        'electionId':   ELECTION_ID,
        'requestURI':   f'/electioninfo/{ELECTION_ID}/vc/vccp09.jsp',
        'topMenuId':    'VC',
        'secondMenuId': 'VCCP09',
        'menuId':       'VCCP09',
        'statementId':  f'{ELECTION_ID}.VCCP09_#{election_code}',
        'electionType': '4',
        'electionCode': str(election_code),
        'cityCode':     str(city_code),
    }
    r = sess.post(f'{BASE_URL}/electioninfo/electionInfo_report.xhtml', data=post_data, timeout=20)
    r.raise_for_status()
    return parse_html(r.text, election_code, city_code)


def parse_html(html, election_code, city_code):
    soup = BeautifulSoup(html, 'html.parser')
    result = {
        'election_type': ELECTION_TYPES.get(str(election_code), ''),
        'city':          CITIES.get(str(city_code), str(city_code)),
        'timestamp':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'candidates':    [],
        'districts':     [],
        'status':        'ok',
    }

    if soup.find(string=lambda t: t and '조회 자료가 없습니다' in t):
        result['status'] = 'no_data'
        result['message'] = '개표 자료가 없습니다.'
        return result

    table = soup.find('table', class_='table01') or soup.find('table')
    if not table:
        result['status'] = 'no_data'
        result['message'] = '테이블 없음'
        return result

    rows = table.find_all('tr')

    # 후보자 헤더 파싱
    candidates_found = []
    skip_words = ['구시군명', '선거인수', '투표수', '무효', '기권', '계', '득표율', '후보자', '선거구명', '정당명']
    for row in rows:
        for th in row.find_all('th'):
            lines = [l.strip() for l in th.get_text(separator='\n').split('\n') if l.strip()]
            if int(th.get('rowspan', 1)) == 1 and int(th.get('colspan', 1)) == 1 and len(lines) >= 2:
                if not any(w in lines[0] for w in skip_words):
                    candidates_found.append({'party': lines[0], 'name': lines[1]})
    result['candidates'] = candidates_found

    # 데이터 행 파싱 (득표수 행 + 득표율 행 쌍)
    def to_int(s):
        try: return int(str(s).replace(',', ''))
        except: return 0
    def to_float(s):
        try: return float(s)
        except: return 0.0

    data_rows = [r for r in rows if r.find('td') and not r.find('th')]
    i = 0
    while i < len(data_rows):
        cells = data_rows[i].find_all('td')
        if not cells: i += 1; continue
        name = cells[0].get_text(strip=True)
        if not name or name.replace(',', '').isdigit(): i += 1; continue

        raw = [c.get_text(strip=True).replace(',', '') for c in cells[1:]]
        pct_raw = []
        if i + 1 < len(data_rows):
            nc = data_rows[i+1].find_all('td')
            nv = [c.get_text(strip=True) for c in nc]
            first = nv[0] if nv else ''
            if not first or first.replace('.','').replace(',','').isdigit():
                pct_raw = nv[1:] if first == '' else nv
                i += 2
            else:
                i += 1
        else:
            i += 1

        voter  = to_int(raw[0]) if len(raw) > 0 else 0
        total  = to_int(raw[1]) if len(raw) > 1 else 0
        cands  = []
        for j, cand in enumerate(candidates_found):
            idx = j + 2
            cands.append({
                'party': cand['party'],
                'name':  cand['name'],
                'votes': to_int(raw[idx]) if idx < len(raw) else 0,
                'pct':   to_float(pct_raw[j]) if j < len(pct_raw) else 0.0,
            })
        result['districts'].append({'name': name, 'voter_count': voter, 'total_votes': total, 'candidates': cands})
    return result


# ── GBoard 호환 API (/api/gboard) ───────────────────────────
@app.route('/api/gboard')
def api_gboard():
    """
    GBoard crawl 모드용 JSON 반환.
    형식: [{ "id": 0, "countingRate": 64.5, "status": "개표중", "candidates": [...] }, ...]
    """
    result = []
    for dc in GBOARD_DISTRICTS:
        try:
            data = fetch_nec_data(dc['election_code'], dc['city_code'])

            if data.get('status') != 'ok':
                result.append({'id': dc['id'], 'countingRate': 0, 'status': '집계전', 'candidates': []})
                continue

            districts = data.get('districts', [])

            # 행 매칭
            matching = None
            for d in districts:
                nm = d['name']
                rf = dc.get('row_filter')
                sf = dc.get('sub_filter')
                if rf and rf not in nm: continue
                if sf and sf not in nm: continue
                matching = d
                break

            # 비례: row_filter 없으면 합계 또는 첫 행
            if matching is None and dc.get('is_pr') and districts:
                matching = next((d for d in districts if '합계' in d['name']), districts[0])

            # 단순 row_filter fallback
            if matching is None and dc.get('row_filter'):
                for d in districts:
                    if dc['row_filter'] in d['name']:
                        matching = d; break

            if matching is None:
                result.append({'id': dc['id'], 'countingRate': 0, 'status': '집계전', 'candidates': []})
                continue

            total  = matching['total_votes']
            voters = matching['voter_count']
            rate   = round(min(100, total / voters * 100), 1) if voters > 0 and total > 0 else 0
            status = '개표중' if total > 0 else '집계전'

            candidates = [
                {
                    'name':    c['name'],
                    'party':   c['party'],
                    'votes':   c['votes'],
                    'isGreen': '녹색당' in (c.get('party') or ''),
                }
                for c in matching['candidates']
            ]

            result.append({'id': dc['id'], 'countingRate': rate, 'status': status, 'candidates': candidates})

        except Exception as e:
            print(f'[gboard] district {dc["id"]} error: {e}')
            result.append({'id': dc['id'], 'countingRate': 0, 'status': f'오류: {e}', 'candidates': []})

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

def fetch_turnout_data():
    sess = requests.Session()
    sess.headers.update({
        'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Referer':         BASE_URL + '/',
        'Origin':          BASE_URL,
    })
    try:
        sess.get(
            f'{BASE_URL}/main/showDocument.xhtml?electionId={ELECTION_ID}&topMenuId=VR&secondMenuId=VRCP09',
            timeout=15
        )
    except Exception:
        pass

    post_data = {
        'electionId':   ELECTION_ID,
        'requestURI':   f'/electioninfo/{ELECTION_ID}/vr/vrcp09.jsp',
        'topMenuId':    'VR',
        'secondMenuId': 'VRCP09',
        'menuId':       'VRCP09',
        'statementId':  f'{ELECTION_ID}.VRCP09_#1',
        'electionType': '1',
        'cityCode':     '0',
    }
    r = sess.post(f'{BASE_URL}/electioninfo/electionInfo_report.xhtml', data=post_data, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, 'html.parser')
    result = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'rate': None,
        'asOf': None,
        'status': 'ok',
    }

    # 기준 시각 파싱 (예: "오전 10시 기준" 패턴)
    text_all = soup.get_text()
    import re
    m = re.search(r'(오전|오후)?\s*(\d{1,2})시\s*기준', text_all)
    if m:
        result['asOf'] = m.group(0).strip()

    table = soup.find('table', class_='table01') or soup.find('table')
    if not table:
        result['status'] = 'no_data'
        return result

    # 전국 합계 행에서 투표율(%) 추출
    rows = table.find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        row_text = ' '.join(c.get_text(strip=True) for c in cells)
        if '전국' in row_text or '합계' in row_text or (len(cells) >= 3 and result['rate'] is None):
            for cell in cells:
                t = cell.get_text(strip=True).replace(',', '')
                if '.' in t and '%' not in t:
                    try:
                        val = float(t)
                        if 0 <= val <= 100:
                            result['rate'] = val
                            break
                    except Exception:
                        pass
            if result['rate'] is not None:
                break

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
