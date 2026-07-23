from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "matches.json"
HISTORY_PATH = ROOT / "data" / "jc_history.json"
ARCHIVE_PATH = ROOT / "data" / "analysis_archive.json"
CURRENT_URL = "https://trade.500.com/jczq/index.php"
HISTORY_URL = "https://open.500.com/iframe/kaijiang/jczq.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://www.500.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
OUTCOME_LABELS = {"home": "主胜", "draw": "平局", "away": "客胜"}
DEFAULT_BASE_HISTORY_SAMPLE = 21138
DEFAULT_BASE_FINISHED_SAMPLE = 21032
DEFAULT_BASE_HISTORY_START = "2021-07-02"
LEAGUE_ALIASES = {
    "美职足": "美职联",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the GitHub Pages dashboard from 500.com.")
    parser.add_argument("--history-days", type=int, default=10, help="Recent result days to refresh.")
    parser.add_argument("--history-retention", type=int, default=400, help="Days kept for team and league profiles.")
    args = parser.parse_args()

    old_payload = load_json(DATA_PATH, {})
    seed_profiles = normalize_seed_profiles(old_payload.get("seedLeagueProfiles") or old_payload.get("leagueProfiles") or [])
    old_history = load_json(HISTORY_PATH, {"matches": []}).get("matches") or []
    old_archive = load_json(ARCHIVE_PATH, {"matches": []}).get("matches") or []

    warnings: list[str] = []
    current_rows: list[dict[str, Any]] | None
    try:
        current_rows = parse_current_rows(fetch_text(CURRENT_URL))
    except Exception as exc:  # noqa: BLE001
        current_rows = None
        warnings.append(f"500 最新竞彩更新失败，已保留上一版赛事：{exc}")

    new_history: list[dict[str, Any]] = []
    for days_ago in range(1, max(1, args.history_days) + 1):
        day_text = (date.today() - timedelta(days=days_ago)).isoformat()
        try:
            html = fetch_text(HISTORY_URL, {"playid": "2", "d": day_text})
            new_history.extend(parse_history_rows(html, day_text))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{day_text} 历史赛果更新失败：{exc}")

    history = merge_history(old_history, new_history, args.history_retention)
    profiles = build_league_profiles(seed_profiles, history)
    team_profiles = build_team_profiles(history)

    if current_rows is None:
        matches = old_payload.get("matches") or []
        update_status = "failed-preserved"
    else:
        intelligence = collect_match_intelligence(current_rows, old_payload.get("matches") or [], warnings)
        matches = [build_match(row, profiles, team_profiles, intelligence.get(str(row.get("id")))) for row in current_rows]
        update_status = "success" if current_rows else "success-no-current-matches"

    archive = update_analysis_archive(old_archive, matches, history, args.history_retention)

    payload = {
        "meta": build_meta(old_payload, matches, history, update_status, warnings),
        "seedLeagueProfiles": seed_profiles,
        "leagueProfiles": list(profiles.values()),
        "teamProfiles": team_profiles,
        "matches": matches,
    }
    write_json(DATA_PATH, payload)
    write_json(HISTORY_PATH, {"updatedAt": now_iso(), "matches": history})
    write_json(ARCHIVE_PATH, {"updatedAt": now_iso(), "matches": archive})
    print(
        json.dumps(
            {
                "status": update_status,
                "matches": len(matches),
                "history": len(history),
                "leagues": len(profiles),
                "teams": len(team_profiles),
                "archive": len(archive),
                "warnings": warnings[:5],
            },
            ensure_ascii=False,
        )
    )


def fetch_text(url: str, params: dict[str, str] | None = None) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    return content.decode("gb18030", errors="replace")


def fetch_bytes(url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.table: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.in_table = True
            self.table = []
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_table and tag in ("td", "th"):
            self.in_cell = True
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in ("td", "th") and self.in_cell:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.in_row:
            if self.row:
                self.table.append(self.row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            self.tables.append(self.table)
            self.in_table = False


def parse_tables(html: str) -> list[list[list[str]]]:
    parser = TableParser()
    parser.feed(html)
    return parser.tables


def parse_current_rows(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r'<tr class="[^"]*bet-tb-tr[^"]*"(?P<attrs>[^>]*)>(?P<body>.*?)</tr>', re.I | re.S)
    for item in pattern.finditer(html):
        attrs = parse_html_attributes(item.group("attrs"))
        body = item.group("body")
        round_name = attrs.get("data-matchnum", "")
        home = attrs.get("data-homesxname", "").strip()
        away = attrs.get("data-awaysxname", "").strip()
        league = canonical_league(attrs.get("data-simpleleague", "").strip() or "竞彩足球")
        match_date = attrs.get("data-matchdate", "")
        match_time = attrs.get("data-matchtime", "")
        odds = parse_market_odds(body, "nspf") or parse_market_odds(body, "spf")
        if not round_name or not home or not away or len(odds) < 3:
            continue
        rankings = [int(value) for value in re.findall(r'title="排名第(\d+)"', body)]
        rows.append({
            "id": f"500-{attrs.get('data-fixtureid') or round_name}",
            "fixture_id": attrs.get("data-fixtureid", ""),
            "info_match_id": attrs.get("data-infomatchid", ""),
            "home_id": attrs.get("data-homeid", ""),
            "away_id": attrs.get("data-awayid", ""),
            "league": league,
            "match_time": f"{match_date} {match_time}".strip(),
            "round": round_name,
            "home": home,
            "away": away,
            "home_rank": rankings[0] if rankings else None,
            "away_rank": rankings[-1] if len(rankings) >= 2 else None,
            "handicap": float_or_none(attrs.get("data-rangqiu")),
            "home_odds": odds[0],
            "draw_odds": odds[1],
            "away_odds": odds[2],
            "detail_url": f"https://odds.500.com/fenxi/shuju-{attrs.get('data-fixtureid')}.shtml",
        })
    return rows


def parse_html_attributes(value: str) -> dict[str, str]:
    return {key.lower(): html_lib.unescape(raw) for key, raw in re.findall(r'([\w-]+)="([^"]*)"', value or "")}


def parse_market_odds(body: str, market_type: str) -> list[float]:
    values: dict[str, float] = {}
    pattern = rf'data-type="{re.escape(market_type)}"\s+data-value="([310])"\s+data-sp="([\d.]+)"'
    for outcome, price in re.findall(pattern, body or "", re.I):
        values[outcome] = float(price)
    return [values[key] for key in ("3", "1", "0") if key in values]


def parse_history_rows(html: str, business_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reference = datetime.strptime(business_date, "%Y-%m-%d").date()
    for table in parse_tables(html):
        if not table or "赛事编号" not in table[0]:
            continue
        for cells in table[1:]:
            if len(cells) < 14 or not re.match(r"^周.\d{3}$", cells[0] or ""):
                continue
            score = re.findall(r"(\d+)\s*[:：-]\s*(\d+)", cells[6] or "")
            if not score:
                continue
            home_score, away_score = map(int, score[-1])
            rows.append(
                {
                    "id": f"500-history-{business_date}-{cells[0]}",
                    "league": canonical_league(cells[1] or "竞彩足球"),
                    "round": cells[0],
                    "date": normalize_time(cells[2], reference),
                    "home": cells[3].strip(),
                    "away": cells[5].strip(),
                    "homeScore": home_score,
                    "awayScore": away_score,
                    "homeOdds": float_or_none(cells[11]),
                    "drawOdds": float_or_none(cells[12]),
                    "awayOdds": float_or_none(cells[13]),
                }
            )
    return rows


def merge_history(old: list[dict[str, Any]], new: list[dict[str, Any]], retention_days: int) -> list[dict[str, Any]]:
    merged = {str(row.get("id")): row for row in old if row.get("id")}
    merged.update({str(row.get("id")): row for row in new if row.get("id")})
    cutoff = date.today() - timedelta(days=max(30, retention_days))
    output = []
    for row in merged.values():
        try:
            row_date = datetime.strptime(str(row.get("date", ""))[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if row_date >= cutoff:
            output.append(row)
    return sorted(output, key=lambda item: (str(item.get("date")), str(item.get("id"))))


def collect_match_intelligence(
    rows: list[dict[str, Any]],
    old_matches: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    previous = {str(item.get("id")): item.get("intelligence") for item in old_matches if item.get("intelligence")}
    output: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(rows)))) as executor:
        futures = {executor.submit(fetch_match_intelligence, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            match_id = str(row.get("id"))
            try:
                output[match_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                if previous.get(match_id):
                    output[match_id] = previous[match_id]
                else:
                    output[match_id] = empty_intelligence(row, f"实时情报获取失败：{exc}")
                failures.append(f"{row.get('home')}vs{row.get('away')}")
    if failures:
        warnings.append(f"{len(failures)}场实时情报使用保留或降级数据：{'、'.join(failures[:4])}")
    return output


def fetch_match_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    fixture_id = str(row.get("fixture_id") or "")
    if not fixture_id:
        return empty_intelligence(row, "缺少500赛事ID")
    detail_url = str(row.get("detail_url") or f"https://odds.500.com/fenxi/shuju-{fixture_id}.shtml")
    detail_html = fetch_text(detail_url)
    lineups = parse_expected_lineups(detail_html)
    home_en = fetch_team_english_name(str(row.get("home_id") or ""))
    away_en = fetch_team_english_name(str(row.get("away_id") or ""))
    news_query = f'"{home_en or row.get("home")}" vs "{away_en or row.get("away")}" team news injuries lineups'
    news = fetch_bing_news(news_query)

    home_info = lineups.get("home") or empty_team_intelligence(str(row.get("home") or "主队"))
    away_info = lineups.get("away") or empty_team_intelligence(str(row.get("away") or "客队"))
    home_info["team"] = str(row.get("home") or home_info.get("team") or "主队")
    away_info["team"] = str(row.get("away") or away_info.get("team") or "客队")
    home_info["englishName"] = home_en
    away_info["englishName"] = away_en
    home_info["news"] = news_for_team(news, home_info["team"], home_en)
    away_info["news"] = news_for_team(news, away_info["team"], away_en)
    home_info["impactScore"] = team_availability_impact(home_info)
    away_info["impactScore"] = team_availability_impact(away_info)
    home_info["summary"] = team_availability_summary(home_info)
    away_info["summary"] = team_availability_summary(away_info)
    has_lineup = bool(home_info.get("starters") or away_info.get("starters"))
    has_news = bool(news)
    return {
        "status": "updated" if has_lineup or has_news else "limited",
        "lineupStatus": "500预计阵容" if has_lineup else "暂无预计阵容",
        "newsStatus": f"近21天新闻{len(news)}条" if news else "近21天未检索到可靠新闻",
        "fetchedAt": now_iso(),
        "source": "500赛事数据页 + Bing News RSS",
        "sourceUrl": detail_url,
        "newsQuery": news_query,
        "home": home_info,
        "away": away_info,
        "news": news,
        "impactSummary": f"主队阵容影响{home_info['impactScore']:.0%}，客队阵容影响{away_info['impactScore']:.0%}",
        "error": "",
    }


def empty_intelligence(row: dict[str, Any], error: str = "") -> dict[str, Any]:
    return {
        "status": "limited",
        "lineupStatus": "暂无预计阵容",
        "newsStatus": "实时新闻待补充",
        "fetchedAt": now_iso(),
        "source": "500赛事数据页",
        "sourceUrl": str(row.get("detail_url") or ""),
        "home": empty_team_intelligence(str(row.get("home") or "主队")),
        "away": empty_team_intelligence(str(row.get("away") or "客队")),
        "news": [],
        "impactSummary": "暂无可量化阵容影响",
        "error": error,
    }


def empty_team_intelligence(team: str) -> dict[str, Any]:
    return {
        "team": team,
        "englishName": "",
        "formation": "",
        "starters": [],
        "bench": [],
        "injuries": [],
        "suspensions": [],
        "news": [],
        "impactScore": 0.0,
        "summary": "暂无明确伤停名单",
    }


def parse_expected_lineups(page_html: str) -> dict[str, dict[str, Any]]:
    starting_match = re.search(r'<div\s+class="[^"]*\bstarting\b[^"]*">', page_html, re.I)
    if not starting_match:
        return {}
    block_start = starting_match.start()
    block_end = page_html.find('<div class="M_box recommend">', block_start)
    if block_end < 0:
        block_end = page_html.find('<!-- 心水推荐', block_start)
    if block_end < 0:
        block_end = min(len(page_html), block_start + 30000)
    lineup_block = page_html[block_start:block_end]
    home_start = lineup_block.find('<div class="team_a">')
    away_start = lineup_block.find('<div class="team_b">', home_start + 1)
    if home_start < 0 or away_start < 0:
        return {}
    away_end = lineup_block.find('<div class="clearb">', away_start + 1)
    if away_end < 0:
        away_end = len(lineup_block)
    return {
        "home": parse_lineup_team_section(lineup_block[home_start:away_start]),
        "away": parse_lineup_team_section(lineup_block[away_start:away_end]),
    }


def parse_lineup_team_section(section: str) -> dict[str, Any]:
    info = empty_team_intelligence("")
    formation = re.search(r'class="team_name">\s*([^<]*?)阵型:&nbsp;\s*([^<]*)', section, re.I | re.S)
    if formation:
        info["team"] = clean_text(formation.group(1))
        info["formation"] = clean_text(formation.group(2))
    mode = "lineup"
    for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', section, re.I | re.S):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.I | re.S)
        if not cells:
            continue
        texts = [clean_text(cell) for cell in cells]
        joined = " ".join(texts)
        if "首发" in joined and "替补" in joined:
            mode = "lineup"
            continue
        if "伤病" in joined and "停赛" in joined:
            mode = "absence"
            continue
        if mode == "lineup":
            if texts and valid_player_text(texts[0]):
                info["starters"].append(normalize_player_text(texts[0]))
            if len(texts) > 1 and valid_player_text(texts[1]):
                info["bench"].append(normalize_player_text(texts[1]))
        else:
            if texts and valid_player_text(texts[0]):
                info["injuries"].append(normalize_player_text(texts[0]))
            if len(texts) > 1 and valid_player_text(texts[1]):
                info["suspensions"].append(normalize_player_text(texts[1]))
    for key in ("starters", "bench", "injuries", "suspensions"):
        info[key] = list(dict.fromkeys(info[key]))[:18]
    return info


def clean_text(value: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', value or "")
    return re.sub(r'\s+', ' ', html_lib.unescape(text).replace("\xa0", " ")).strip()


def valid_player_text(value: str) -> bool:
    text = (value or "").strip(" -")
    blocked = {"暂无", "无", "-", "总成绩", "主场", "客场", "比赛", "比赛日期", "赛事"}
    if not text or text in blocked or text.startswith("声明"):
        return False
    if re.fullmatch(r"-?\d{1,4}(?:-\d{1,2}){1,2}", text):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text)) and len(text) <= 80


def normalize_player_text(value: str) -> str:
    return re.sub(r'^\d+[.、\s]*', '', (value or "").strip()).strip()


def fetch_team_english_name(team_id: str) -> str:
    if not team_id:
        return ""
    try:
        page = fetch_text(f"https://liansai.500.com/team/{team_id}/")
        match = re.search(r'class="itm_name_en">\s*([^<]+)', page, re.I)
        return clean_text(match.group(1)) if match else ""
    except Exception:  # noqa: BLE001
        return ""


def fetch_bing_news(query: str, limit: int = 4) -> list[dict[str, Any]]:
    headers = {"User-Agent": HEADERS["User-Agent"], "Accept-Language": "en-US,en;q=0.9"}
    raw = fetch_bytes("https://www.bing.com/news/search", {"q": query, "format": "rss"}, headers=headers)
    root = ET.fromstring(raw)
    output: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title") or "")
        description = clean_text(item.findtext("description") or "")[:260]
        link = (item.findtext("link") or "").strip()
        published_text = (item.findtext("pubDate") or "").strip()
        try:
            published = parsedate_to_datetime(published_text)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None
        if not title or not published or published < cutoff:
            continue
        source_node = item.find(".//{*}Source")
        output.append({
            "title": title,
            "description": description,
            "url": link,
            "publishedAt": published.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="minutes"),
            "source": clean_text(source_node.text or "") if source_node is not None else "Bing News",
        })
        if len(output) >= limit:
            break
    return output


def news_for_team(news: list[dict[str, Any]], chinese_name: str, english_name: str) -> list[dict[str, Any]]:
    tokens = [token.lower() for token in (chinese_name, english_name) if token]
    matched = [item for item in news if any(token in f"{item.get('title')} {item.get('description')}".lower() for token in tokens)]
    return (matched or news)[:3]


def team_availability_impact(info: dict[str, Any]) -> float:
    impact = 0.0
    for item in info.get("injuries") or []:
        impact += player_absence_weight(item, suspension=False)
    for item in info.get("suspensions") or []:
        impact += player_absence_weight(item, suspension=True)
    news_text = " ".join(f"{item.get('title')} {item.get('description')}" for item in info.get("news") or []).lower()
    impact += sum(0.025 for word in ("injury", "injured", "ruled out", "doubt", "suspended", "缺席", "伤停") if word in news_text)
    impact -= sum(0.015 for word in ("returns", "return from injury", "fit again", "复出") if word in news_text)
    return round(clamp(impact, 0.0, 0.36), 3)


def player_absence_weight(value: str, suspension: bool) -> float:
    text = value or ""
    weight = 0.055 if suspension else 0.045
    if "守门员" in text:
        weight += 0.035
    elif "前锋" in text:
        weight += 0.025
    elif "中场" in text:
        weight += 0.018
    elif "后卫" in text:
        weight += 0.012
    return weight


def team_availability_summary(info: dict[str, Any]) -> str:
    injuries = info.get("injuries") or []
    suspensions = info.get("suspensions") or []
    starters = info.get("starters") or []
    if injuries or suspensions:
        return f"预计首发{len(starters)}人，伤病{len(injuries)}人，停赛{len(suspensions)}人，影响评分{float(info.get('impactScore') or 0):.0%}"
    if starters:
        return f"预计首发{len(starters)}人，500当前未列出明确伤停或停赛"
    return "暂无预计首发与明确伤停名单，结论已降权"


def build_league_profiles(seed_rows: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seed = {str(row.get("league")): row for row in normalize_seed_profiles(seed_rows) if row.get("league")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history:
        grouped[canonical_league(row.get("league") or "竞彩足球")].append(row)

    profiles: dict[str, dict[str, Any]] = {}
    for league in sorted(set(seed) | set(grouped)):
        recent = rates_from_history(grouped.get(league, []))
        base = seed.get(league) or {}
        recent_weight = min(recent["sample"], 120) * 2
        base_weight = min(int(base.get("sample") or 0), 300)
        if recent_weight and base_weight:
            home_rate = weighted(base.get("homeRate"), base_weight, recent["homeRate"], recent_weight)
            draw_rate = weighted(base.get("drawRate"), base_weight, recent["drawRate"], recent_weight)
            away_rate = weighted(base.get("awayRate"), base_weight, recent["awayRate"], recent_weight)
            favorite_hit = weighted(base.get("favoriteHitRate"), base_weight, recent["favoriteHitRate"], recent_weight)
        elif recent_weight:
            home_rate, draw_rate, away_rate = recent["homeRate"], recent["drawRate"], recent["awayRate"]
            favorite_hit = recent["favoriteHitRate"]
        else:
            home_rate = float(base.get("homeRate") or 0.42)
            draw_rate = float(base.get("drawRate") or 0.28)
            away_rate = float(base.get("awayRate") or 0.30)
            favorite_hit = float(base.get("favoriteHitRate") or 0.55)
        normalized = normalize({"home": home_rate, "draw": draw_rate, "away": away_rate})
        profiles[league] = {
            "league": league,
            "sample": int(base.get("sample") or 0) + recent["sample"],
            "recentSample": recent["sample"],
            "homeRate": normalized["home"],
            "drawRate": normalized["draw"],
            "awayRate": normalized["away"],
            "topOutcome": OUTCOME_LABELS[max(normalized, key=normalized.get)],
            "topRate": max(normalized.values()),
            "favoriteHitRate": clamp(favorite_hit, 0, 1),
            "kellyHitRate": float(base.get("kellyHitRate") or 0),
            "over25Rate": recent["over25Rate"],
            "reliable": bool(base.get("reliable")) or recent["sample"] >= 30,
            "source": "Interwetten五年基线 + 500竞彩每日增量",
        }
    if not profiles:
        profiles["竞彩足球"] = default_profile("竞彩足球")
    return profiles


def rates_from_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    results = Counter()
    favorite_total = 0
    favorite_hits = 0
    over = 0
    for row in rows:
        home_score = int(row.get("homeScore") or 0)
        away_score = int(row.get("awayScore") or 0)
        actual = "home" if home_score > away_score else ("away" if home_score < away_score else "draw")
        results[actual] += 1
        over += int(home_score + away_score >= 3)
        odds = {"home": row.get("homeOdds"), "draw": row.get("drawOdds"), "away": row.get("awayOdds")}
        valid = {key: value for key, value in odds.items() if isinstance(value, (int, float)) and value > 1}
        if len(valid) == 3:
            favorite_total += 1
            favorite_hits += int(min(valid, key=valid.get) == actual)
    denominator = max(total, 1)
    return {
        "sample": total,
        "homeRate": results["home"] / denominator,
        "drawRate": results["draw"] / denominator,
        "awayRate": results["away"] / denominator,
        "favoriteHitRate": favorite_hits / max(favorite_total, 1) if favorite_total else 0.55,
        "over25Rate": over / denominator if total else 0.50,
    }


def build_team_profiles(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "team": "", "league": "", "played": 0, "wins": 0, "draws": 0, "losses": 0,
        "homePlayed": 0, "homeWins": 0, "homeDraws": 0, "homeLosses": 0,
        "awayPlayed": 0, "awayWins": 0, "awayDraws": 0, "awayLosses": 0,
        "goalsFor": 0, "goalsAgainst": 0,
    })
    for row in history:
        home = str(row.get("home") or "").strip()
        away = str(row.get("away") or "").strip()
        if not home or not away:
            continue
        hs, aas = int(row.get("homeScore") or 0), int(row.get("awayScore") or 0)
        update_team(stats[normalize_team(home)], home, str(row.get("league") or ""), True, hs, aas)
        update_team(stats[normalize_team(away)], away, str(row.get("league") or ""), False, aas, hs)
    output: dict[str, dict[str, Any]] = {}
    for key, row in stats.items():
        played = max(row["played"], 1)
        home_played = max(row["homePlayed"], 1)
        away_played = max(row["awayPlayed"], 1)
        output[key] = {
            "team": row["team"], "league": row["league"], "sample": row["played"],
            "winRate": row["wins"] / played, "drawRate": row["draws"] / played, "lossRate": row["losses"] / played,
            "homeWinRate": row["homeWins"] / home_played, "homeDrawRate": row["homeDraws"] / home_played,
            "homeLossRate": row["homeLosses"] / home_played, "awayWinRate": row["awayWins"] / away_played,
            "awayDrawRate": row["awayDraws"] / away_played, "awayLossRate": row["awayLosses"] / away_played,
            "goalsFor": row["goalsFor"] / played, "goalsAgainst": row["goalsAgainst"] / played,
        }
    return output


def update_team(row: dict[str, Any], name: str, league: str, is_home: bool, goals_for: int, goals_against: int) -> None:
    row["team"], row["league"] = name, league
    row["played"] += 1
    row["goalsFor"] += goals_for
    row["goalsAgainst"] += goals_against
    outcome = "Wins" if goals_for > goals_against else ("Losses" if goals_for < goals_against else "Draws")
    row[outcome.lower()] += 1
    prefix = "home" if is_home else "away"
    row[f"{prefix}Played"] += 1
    row[f"{prefix}{outcome}"] += 1


def build_match(
    row: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    teams: dict[str, dict[str, Any]],
    intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    league = str(row.get("league") or "竞彩足球")
    profile = profiles.get(league) or average_profile(profiles)
    intelligence = intelligence or empty_intelligence(row)
    market = implied_probabilities(row["home_odds"], row["draw_odds"], row["away_odds"])
    probs = normalize({
        "home": market["home"] * 0.82 + profile["homeRate"] * 0.18,
        "draw": market["draw"] * 0.82 + profile["drawRate"] * 0.18,
        "away": market["away"] * 0.82 + profile["awayRate"] * 0.18,
    })
    probs, intelligence_adjustment = apply_intelligence_adjustment(probs, intelligence)
    ranked = sorted(probs, key=probs.get, reverse=True)
    primary_key, upset_key = ranked[0], ranked[1]
    primary, upset_direction = OUTCOME_LABELS[primary_key], OUTCOME_LABELS[upset_key]
    gap = probs[primary_key] - probs[upset_key]
    favorite_fail = 1 - float(profile.get("favoriteHitRate") or 0.55)
    favorite_odds = min(row["home_odds"], row["draw_odds"], row["away_odds"])
    cold_risk = clamp((1 - probs[primary_key]) * 0.72 + probs[upset_key] * 0.22 + favorite_fail * 0.18, 0.18, 0.84)
    if favorite_odds >= 2.40:
        cold_risk = clamp(cold_risk + 0.07, 0, 0.88)
    if gap <= 0.05:
        cold_risk = clamp(cold_risk + 0.06, 0, 0.90)
    risk_level = "高" if cold_risk >= 0.66 else ("中" if cold_risk >= 0.52 else ("低中" if cold_risk >= 0.40 else "低"))
    anti_draw_value = anti_draw_score(probs, profile, gap, cold_risk, upset_key)
    draw_defense = draw_defense_plan(probs, profile, primary_key, upset_key, gap, cold_risk, anti_draw_value, favorite_odds)
    protection_key = upset_key if cold_risk >= 0.55 else ("draw" if draw_defense["cover"] else primary_key)
    if draw_defense["cover"] and primary_key != "draw" and protection_key != "draw":
        draw_defense = {
            **draw_defense,
            "cover": False,
            "level": "watch",
            "label": "平局观察",
            "verdict": "平局观察",
            "action": "平局作为第三风险提示",
            "reasons": [*draw_defense["reasons"][:4], "第二方向风险更高，正式保护位优先留给第二方向"],
        }
    anti_draw_verdict = draw_defense["verdict"]
    scores, cold_scores, expected_total, open_game = generate_scores(
        probs, profile, primary_key, upset_key, cold_risk, row, teams, protection_key
    )
    score_totals = [sum(int(part) for part in item["score"].split("-")) for item in scores]
    over_under = "大2.5球倾向" if open_game and all(total >= 3 for total in score_totals) else ("小2.5球倾向" if all(total <= 2 for total in score_totals) else "2/3球临界")
    reliability_bonus = 6 if profile.get("reliable") else 2
    confidence = round(clamp(50 + gap * 70 + (1 - cold_risk) * 15 + reliability_bonus, 45, 90))
    if draw_defense["level"] == "must":
        confidence = min(confidence - 4, 82)
    elif draw_defense["cover"]:
        confidence = min(confidence - 2, 86)
    protection_direction = OUTCOME_LABELS[protection_key]
    cover = primary if protection_key == primary_key else f"{primary}，防{protection_direction}"
    action = "重点候选" if confidence >= 72 and cold_risk < 0.60 else (f"防冷优先：{cover}" if cold_risk >= 0.66 else "观察复核")
    value_gate = "通过：进入候选观察池" if action == "重点候选" else ("高风险：只做防冷复盘" if cold_risk >= 0.66 else "观察：等待临场确认")
    if protection_key == "draw" and primary_key != "draw":
        defend = "强制提示：防平局" if draw_defense["level"] == "must" else "建议保留：防平局"
    else:
        defend = f"强制提示：防{upset_direction}" if cold_risk >= 0.66 else (f"建议保留：防{upset_direction}" if cold_risk >= 0.52 else f"轻度观察：{upset_direction}")
    score_text = "、".join(item["score"] for item in scores)
    final = f"500竞彩赔率与联赛画像统一倾向为{primary}，信心指数{confidence}/100；"
    if protection_key == "draw" and primary_key != "draw":
        final += f"平局防守等级为{draw_defense['label']}，执行口径为{cover}；"
    elif cold_risk >= 0.52:
        final += f"爆冷评分{cold_risk:.1%}，重点防{upset_direction}；"
    final += f"两个最得意比分为{score_text}，{over_under}。"
    if cold_scores:
        final += f" 大球或比赛失控时，爆冷比分留意{'、'.join(item['score'] for item in cold_scores)}。"
    customer_summary = build_customer_summary(
        row, probs, primary, protection_direction, cover, confidence, over_under, scores, cold_scores, intelligence,
        draw_defense,
    )

    odds = {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"]}
    agents = build_agents(row, profile, probs, cold_risk, risk_level, anti_draw_value, anti_draw_verdict, scores, cold_scores, confidence, cover, intelligence)
    return {
        "id": row["id"], "date": row["match_time"], "round": row["round"], "league": league,
        "home": row["home"], "away": row["away"], "sourceType": "500-jc",
        "fixtureId": row.get("fixture_id"), "homeTeamId": row.get("home_id"), "awayTeamId": row.get("away_id"),
        "homeRank": row.get("home_rank"), "awayRank": row.get("away_rank"), "handicap": row.get("handicap"),
        "odds": {
            "current": odds, "initial": odds, "shape": f"{row['home_odds']}/{row['draw_odds']}/{row['away_odds']}",
            "movement": "500竞彩即时SP", "movementCombo": "500最新竞彩赔率 + 历史联赛校准",
            "favorite": OUTCOME_LABELS[min(odds, key=odds.get)], "favoriteOdds": favorite_odds,
            "favoriteChange": "等待临场变化", "gap": abs(row["home_odds"] - row["away_odds"]),
            "gapLabel": "均势盘" if abs(row["home_odds"] - row["away_odds"]) <= 0.5 else "强弱分层",
            "mode": "500竞彩即时盘", "dropSide": "待临场", "dropBucket": "即时快照",
        },
        "probabilities": probs, "leagueProfile": profile,
        "intelligence": intelligence,
        "intelligenceAdjustment": intelligence_adjustment,
        "grid": {"signal": "500 SP基线", "roi": None, "sample": profile.get("sample"), "bucket": "JCToday/Live"},
        "upset": {
            "score": cold_risk, "level": risk_level, "direction": upset_direction,
            "trap": f"热门赔率{favorite_odds:.2f}；主方向领先{gap:.1%}；联赛热门失手率{favorite_fail:.1%}",
            "reasons": [f"综合非主方向概率{1-probs[primary_key]:.1%}", f"第二方向{upset_direction}{probs[upset_key]:.1%}", f"联赛样本{int(profile.get('sample') or 0)}场"],
        },
        "antiDraw": {
            "score": anti_draw_value, "verdict": anti_draw_verdict,
            "action": draw_defense["action"],
            "reasons": draw_defense["reasons"],
        },
        "conclusion": {
            "action": action, "primary": primary, "cover": cover, "confidence": confidence, "valueGate": value_gate,
            "bestScores": scores, "coldScores": cold_scores, "overUnder": over_under, "openGame": open_game,
            "defendCold": defend, "finalText": final, "customerSummary": customer_summary,
            "riskNotice": f"爆冷可能性{risk_level}，当前执行防守方向为{protection_direction}。",
        },
        "agents": agents,
    }


def build_agents(row: dict[str, Any], profile: dict[str, Any], probs: dict[str, float], cold: float, level: str,
                 anti: int, anti_verdict: str, scores: list[dict[str, Any]], cold_scores: list[dict[str, Any]],
                 confidence: int, cover: str, intelligence: dict[str, Any]) -> list[dict[str, Any]]:
    primary = OUTCOME_LABELS[max(probs, key=probs.get)]
    home_info = intelligence.get("home") or {}
    away_info = intelligence.get("away") or {}
    news = intelligence.get("news") or []
    score_signal = " / ".join(item["score"] for item in scores)
    if cold_scores:
        score_signal += "；冷门 " + " / ".join(item["score"] for item in cold_scores)
    return [
        agent("数据底座Agent", "500实时校验", 92, f"已读取500竞彩编号{row['round']}，主平客SP完整。"),
        agent("近期状态Agent", "球队近一年画像", max(48, confidence - 5), f"主客排名{row.get('home_rank') or '--'} / {row.get('away_rank') or '--'}；历史胜平负与进球均值已经进入概率层。"),
        agent("阵容伤停Agent", intelligence.get("lineupStatus") or "阵容待定", round(100 - max(float(home_info.get('impactScore') or 0), float(away_info.get('impactScore') or 0)) * 100), f"主队：{home_info.get('summary') or '暂无'}；客队：{away_info.get('summary') or '暂无'}。阵容为预计状态，临场名单公布后需复核。"),
        agent("实时新闻Agent", intelligence.get("newsStatus") or "新闻待补充", 78 if news else 48, "；".join(item.get("title", "") for item in news[:2]) or "近21天未检索到可验证的阵容新闻，不编造球员消息。"),
        agent("联赛画像Agent", profile.get("topOutcome") or "联赛基准", 84 if profile.get("reliable") else 62, f"{profile['league']}累计画像{int(profile.get('sample') or 0)}场，近期增量{int(profile.get('recentSample') or 0)}场。"),
        agent("Interwetten赔率Agent", "500 SP + 历史基线", 78, f"即时赔率{row['home_odds']}/{row['draw_odds']}/{row['away_odds']}，已与Interwetten历史画像交叉校准。"),
        agent("爆冷防线Agent", level, round(cold * 100), f"爆冷评分{cold:.1%}，第二方向为{OUTCOME_LABELS[sorted(probs, key=probs.get, reverse=True)[1]]}。"),
        agent("反防平Agent", anti_verdict, anti, f"平局模型{probs['draw']:.1%}，联赛基准{profile['drawRate']:.1%}。"),
        agent("比分脚本Agent", score_signal, confidence, "常规比分与大球爆冷比分分层输出，由方向概率、进球基线、阵容影响和Poisson矩阵联合筛选。"),
        agent("圆桌仲裁Agent", cover, confidence, f"多Agent完成统一仲裁，主方向{primary}。"),
    ]


def agent(name: str, signal: str, score: int, view: str) -> dict[str, Any]:
    return {"name": name, "status": "已执行", "signal": signal, "score": score, "view": view}


def anti_draw_score(probs: dict[str, float], profile: dict[str, Any], gap: float, cold: float, upset_key: str) -> int:
    score = 48 + (0.255 - probs["draw"]) * 120 + max(0.0, gap - 0.20) * 55 - cold * 14
    score += (0.255 - float(profile.get("drawRate") or 0.255)) * 40
    if upset_key == "draw":
        score -= 16
    return round(clamp(score, 0, 100))


def draw_defense_plan(
    probs: dict[str, float],
    profile: dict[str, Any],
    primary_key: str,
    upset_key: str,
    gap: float,
    cold: float,
    anti_draw: int,
    favorite_odds: float | None,
) -> dict[str, Any]:
    draw_prob = float(probs.get("draw") or 0)
    league_draw = float(profile.get("drawRate") or 0.255)
    primary_prob = float(probs.get(primary_key) or 0)
    draw_is_second = upset_key == "draw"
    reasons = [f"模型平局概率{draw_prob:.1%}", f"联赛平局基准{league_draw:.1%}", f"主方向领先{gap:.1%}"]

    strong_no_draw = (
        primary_key != "draw"
        and primary_prob >= 0.66
        and draw_prob <= 0.20
        and league_draw <= 0.25
        and gap >= 0.42
        and cold < 0.42
        and (favorite_odds is None or favorite_odds <= 1.45)
    )
    must_cover = primary_key != "draw" and (
        draw_prob >= 0.285
        or (draw_is_second and draw_prob >= 0.23 and league_draw >= 0.28)
        or (draw_is_second and draw_prob >= 0.24 and gap <= 0.30)
        or (draw_is_second and anti_draw <= 38)
    )
    should_cover = primary_key != "draw" and not strong_no_draw and (
        must_cover
        or (draw_is_second and draw_prob >= 0.215)
        or (league_draw >= 0.30 and draw_prob >= 0.205)
        or (draw_prob >= 0.255 and gap <= 0.34)
        or anti_draw <= 52
    )

    if primary_key == "draw":
        level, label, verdict, action = "primary", "平局主方向", "平局为主方向", "平局进入主判断层"
    elif must_cover:
        level, label, verdict, action = "must", "必须防平", "必须防平", "平局进入正式防守层"
        reasons.append("平局同时得到概率排序或联赛高平局画像支持")
    elif should_cover:
        level, label, verdict, action = "suggest", "建议防平", "建议防平", "主方向 + 平局保护"
        reasons.append("平局已进入第二风险层，不再用高信心直接排除")
    else:
        level, label, verdict, action = "none", "有依据不防平", "有依据不防平", "单方向成立"
        reasons.append("强胜概率、低平局概率与低平联赛条件共同成立")
    return {
        "cover": bool(should_cover or must_cover),
        "level": level,
        "label": label,
        "verdict": verdict,
        "action": action,
        "reasons": reasons[:5],
    }


def apply_intelligence_adjustment(
    probabilities: dict[str, float],
    intelligence: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    home_impact = float((intelligence.get("home") or {}).get("impactScore") or 0)
    away_impact = float((intelligence.get("away") or {}).get("impactScore") or 0)
    directional_shift = clamp((away_impact - home_impact) * 0.12, -0.045, 0.045)
    uncertainty = clamp((home_impact + away_impact) * 0.035, 0.0, 0.022)
    adjusted = normalize({
        "home": probabilities["home"] + directional_shift - uncertainty / 2,
        "draw": probabilities["draw"] + uncertainty,
        "away": probabilities["away"] - directional_shift - uncertainty / 2,
    })
    if abs(directional_shift) < 0.004 and uncertainty < 0.004:
        summary = "当前伤停没有形成足以改变主方向的量化差值"
    elif directional_shift > 0:
        summary = f"客队伤停压力更高，主胜概率上调约{directional_shift:.1%}"
    else:
        summary = f"主队伤停压力更高，客胜概率上调约{abs(directional_shift):.1%}"
    return adjusted, {
        "homeImpact": home_impact,
        "awayImpact": away_impact,
        "directionalShift": round(directional_shift, 4),
        "drawUncertainty": round(uncertainty, 4),
        "summary": summary,
    }


def build_customer_summary(
    row: dict[str, Any],
    probs: dict[str, float],
    primary: str,
    secondary_direction: str,
    cover: str,
    confidence: int,
    over_under: str,
    scores: list[dict[str, Any]],
    cold_scores: list[dict[str, Any]],
    intelligence: dict[str, Any],
    draw_defense: dict[str, Any],
) -> dict[str, Any]:
    home_info = intelligence.get("home") or {}
    away_info = intelligence.get("away") or {}
    ranks = ""
    if row.get("home_rank") or row.get("away_rank"):
        ranks = f"联赛排名约{row.get('home_rank') or '--'}位对{row.get('away_rank') or '--'}位。"
    lineup = f"主队{home_info.get('summary') or '阵容待定'}；客队{away_info.get('summary') or '阵容待定'}。"
    news = intelligence.get("news") or []
    news_line = f"最新情报：{news[0].get('title')}。" if news else "近21天未检索到可验证的阵容新闻，临场首发仍需复核。"
    score_line = "、".join(item["score"] for item in scores)
    cold_line = "、".join(item["score"] for item in cold_scores)
    draw_note = ""
    if draw_defense.get("cover"):
        draw_note = f"本场{draw_defense.get('label')}，信心仅代表主方向优势，不代表排除平局。"
    analysis = (
        f"赔率、联赛画像和阵容影响合并后，{primary}概率最高，信心{confidence}/100；"
        f"次选为{secondary_direction}，当前执行口径为{cover}。{draw_note}{ranks}{lineup}{news_line}"
    )
    return {
        "headline": f"主看{primary}，次选{secondary_direction}",
        "primary": primary,
        "secondary": secondary_direction,
        "cover": cover,
        "confidence": confidence,
        "overUnder": over_under,
        "mainScores": score_line,
        "coldScores": cold_line,
        "probabilityLine": f"主胜{probs['home']:.1%} · 平局{probs['draw']:.1%} · 客胜{probs['away']:.1%}",
        "analysis": analysis,
    }


def generate_scores(probs: dict[str, float], profile: dict[str, Any], primary: str, upset: str, cold: float,
                    row: dict[str, Any], teams: dict[str, dict[str, Any]],
                    protection_key: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, bool]:
    home_team = teams.get(normalize_team(row["home"])) or {}
    away_team = teams.get(normalize_team(row["away"])) or {}
    over_rate = float(profile.get("over25Rate") or 0.50)
    total = 2.35 + (over_rate - 0.50) * 1.4
    if home_team and away_team:
        observed_total = (float(home_team.get("goalsFor") or 1.2) + float(home_team.get("goalsAgainst") or 1.2) +
                          float(away_team.get("goalsFor") or 1.2) + float(away_team.get("goalsAgainst") or 1.2)) / 2
        total = total * 0.65 + clamp(observed_total, 1.6, 3.8) * 0.35
    edge = clamp((probs["home"] - probs["away"]) * 1.35, -0.85, 0.85)
    home_lambda = clamp(total / 2 + 0.16 + edge, 0.25, 3.6)
    away_lambda = clamp(total - home_lambda, 0.20, 3.2)
    expected_total = home_lambda + away_lambda
    open_game = expected_total >= 2.72 or over_rate >= 0.61
    candidates = []
    for home_goals in range(6):
        for away_goals in range(6):
            outcome = "home" if home_goals > away_goals else ("away" if home_goals < away_goals else "draw")
            candidates.append({
                "score": f"{home_goals}-{away_goals}",
                "probability": poisson(home_goals, home_lambda) * poisson(away_goals, away_lambda),
                "outcome": outcome,
            })
    primary_rows = sorted((item for item in candidates if item["outcome"] == primary), key=lambda item: item["probability"], reverse=True)
    cover_key = protection_key or (upset if cold >= 0.55 else primary)
    cover_rows = sorted((item for item in candidates if item["outcome"] == cover_key), key=lambda item: item["probability"], reverse=True)
    if open_game:
        first = next((item for item in primary_rows if score_total(item["score"]) >= 3), primary_rows[0])
    else:
        first = primary_rows[0]
    selected = [first]
    if cover_key != primary:
        second = next(
            (item for item in cover_rows if item["score"] != first["score"] and (not open_game or score_total(item["score"]) >= 3)),
            next((item for item in cover_rows if item["score"] != first["score"]), None),
        )
    elif open_game:
        second = next((item for item in primary_rows if item["score"] != first["score"] and score_total(item["score"]) >= 3), None)
    else:
        second = None
    selected.append(second or next((item for item in cover_rows if item["score"] != first["score"]), primary_rows[1]))
    output = [{"score": item["score"], "probability": round(item["probability"], 4),
               "script": "防冷脚本" if item["outcome"] != primary else ("开放局脚本" if score_total(item["score"]) >= 3 else "主方向脚本")} for item in selected]

    cold_pool = sorted(
        (item for item in candidates if item["outcome"] == upset and (not open_game or score_total(item["score"]) >= 3)),
        key=lambda item: item["probability"],
        reverse=True,
    )
    cold_limit = 2 if open_game else (1 if cold >= 0.58 else 0)
    selected_scores = {selected_item["score"] for selected_item in selected}
    available_cold = [item for item in cold_pool if item["score"] not in selected_scores]
    cold_output = [
        {"score": item["score"], "probability": round(item["probability"], 4), "script": "大球爆冷脚本" if open_game else "爆冷脚本"}
        for item in available_cold[:cold_limit]
    ]
    return output, cold_output, expected_total, open_game


def score_total(score: str) -> int:
    try:
        return sum(int(part) for part in score.split("-", 1))
    except (TypeError, ValueError):
        return 0


def implied_probabilities(home: float, draw: float, away: float) -> dict[str, float]:
    return normalize({"home": 1 / home, "draw": 1 / draw, "away": 1 / away})


def average_profile(profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = list(profiles.values())
    if not values:
        return default_profile("综合联赛")
    total = sum(max(int(item.get("sample") or 0), 1) for item in values)
    profile = default_profile("综合联赛")
    for key in ("homeRate", "drawRate", "awayRate", "favoriteHitRate", "over25Rate"):
        profile[key] = sum(float(item.get(key) or profile[key]) * max(int(item.get("sample") or 0), 1) for item in values) / total
    profile["sample"] = total
    profile["reliable"] = True
    return profile


def default_profile(league: str) -> dict[str, Any]:
    return {"league": league, "sample": 0, "recentSample": 0, "homeRate": 0.42, "drawRate": 0.28,
            "awayRate": 0.30, "topOutcome": "主胜", "topRate": 0.42, "favoriteHitRate": 0.55,
            "kellyHitRate": 0, "over25Rate": 0.50, "reliable": False, "source": "综合基线"}


def update_analysis_archive(
    old_archive: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    history: list[dict[str, Any]],
    retention_days: int,
) -> list[dict[str, Any]]:
    archive = {str(item.get("id")): dict(item) for item in old_archive if item.get("id")}
    for match in matches:
        conclusion = match.get("conclusion") or {}
        archive[str(match.get("id"))] = {
            "id": str(match.get("id")),
            "date": match.get("date"),
            "round": match.get("round"),
            "league": match.get("league"),
            "home": match.get("home"),
            "away": match.get("away"),
            "predictedPrimary": conclusion.get("primary"),
            "cover": conclusion.get("cover"),
            "confidence": conclusion.get("confidence"),
            "bestScores": conclusion.get("bestScores") or [],
            "coldScores": conclusion.get("coldScores") or [],
            "overUnder": conclusion.get("overUnder"),
            "upsetScore": (match.get("upset") or {}).get("score"),
            "customerSummary": conclusion.get("customerSummary") or {},
            "createdAt": (archive.get(str(match.get("id"))) or {}).get("createdAt") or now_iso(),
            "updatedAt": now_iso(),
            "finalScore": (archive.get(str(match.get("id"))) or {}).get("finalScore") or "",
            "actualOutcome": (archive.get(str(match.get("id"))) or {}).get("actualOutcome") or "",
            "directionHit": (archive.get(str(match.get("id"))) or {}).get("directionHit"),
        }

    finished_by_teams: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history:
        key = f"{normalize_team(row.get('home'))}|{normalize_team(row.get('away'))}"
        finished_by_teams[key].append(row)
    cutoff = date.today() - timedelta(days=max(30, retention_days))
    output: list[dict[str, Any]] = []
    for item in archive.values():
        try:
            match_date = datetime.strptime(str(item.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if match_date < cutoff:
            continue
        key = f"{normalize_team(item.get('home'))}|{normalize_team(item.get('away'))}"
        candidates = finished_by_teams.get(key) or []
        result = min(
            candidates,
            key=lambda row: abs((safe_date(str(row.get("date") or "")) - match_date).days),
            default=None,
        )
        if result and abs((safe_date(str(result.get("date") or "")) - match_date).days) <= 3:
            home_score = int(result.get("homeScore") or 0)
            away_score = int(result.get("awayScore") or 0)
            actual = "主胜" if home_score > away_score else ("客胜" if home_score < away_score else "平局")
            item["finalScore"] = f"{home_score}-{away_score}"
            item["actualOutcome"] = actual
            item["directionHit"] = item.get("predictedPrimary") == actual
            item["finishedAt"] = result.get("date")
        output.append(item)
    return sorted(output, key=lambda item: str(item.get("date") or ""), reverse=True)


def safe_date(value: str) -> date:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return date(1970, 1, 1)


def build_meta(old_payload: dict[str, Any], matches: list[dict[str, Any]], history: list[dict[str, Any]],
               status: str, warnings: list[str]) -> dict[str, Any]:
    old_meta = old_payload.get("meta") or {}
    dates = [str(row.get("date") or "")[:10] for row in history if row.get("date")]
    base_history_sample = DEFAULT_BASE_HISTORY_SAMPLE
    base_finished_sample = DEFAULT_BASE_FINISHED_SAMPLE
    history_end = max(dates) if dates else str(old_meta.get("generatedAt") or "")[:10]
    return {
        "product": "AI足球赛事研判指挥舱 HTML授权版",
        "modelName": "Local AI 5.5",
        "generatedAt": now_iso(),
        "source": "500彩票网竞彩日常更新 + Interwetten五年历史画像",
        "matchCount": len(matches),
        "baseHistorySample": base_history_sample,
        "baseFinishedSample": base_finished_sample,
        "historySample": base_history_sample + len(history),
        "finishedSample": base_finished_sample + len(history),
        "historyRange": f"{DEFAULT_BASE_HISTORY_START} 至 {history_end}" if history_end else old_meta.get("historyRange", ""),
        "dailyUpdate": {"status": status, "historyRows": len(history), "warnings": warnings[:12]},
        "compliance": "仅提供体育数据分析、赛前研究与内容创作参考；不涉及赌博，不提供下注服务，不承诺结果。",
    }


def split_teams(value: str) -> tuple[str, str]:
    cleaned = re.sub(r"\[[^\]]+\]", "", value or "").strip()
    parts = re.split(r"\s+VS\s+|vs|VS", cleaned)
    return (parts[0].strip(), parts[1].strip()) if len(parts) >= 2 else ("", "")


def normalize_time(value: str, reference: date) -> str:
    match = re.search(r"(\d{2})-(\d{2})\s+(\d{2}:\d{2})", value or "")
    if not match:
        return (value or "").strip()
    month, day, hour_minute = int(match.group(1)), int(match.group(2)), match.group(3)
    parsed = date(reference.year, month, day)
    if (parsed - reference).days > 180:
        parsed = date(reference.year - 1, month, day)
    elif (reference - parsed).days > 180:
        parsed = date(reference.year + 1, month, day)
    return f"{parsed:%Y-%m-%d} {hour_minute}"


def canonical_league(value: Any) -> str:
    league = str(value or "竞彩足球").strip() or "竞彩足球"
    return LEAGUE_ALIASES.get(league, league)


def normalize_seed_profiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        league = canonical_league(row.get("league"))
        candidate = dict(row)
        candidate["league"] = league
        existing = merged.get(league)
        if existing is None or int(candidate.get("sample") or 0) > int(existing.get("sample") or 0):
            merged[league] = candidate
    return list(merged.values())


def normalize_team(value: str) -> str:
    return re.sub(r"[\s·.\-（）()\[\]]+", "", str(value or "")).lower()


def normalize(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(float(value or 0), 0) for value in values.values()) or 1
    return {key: max(float(value or 0), 0) / total for key, value in values.items()}


def weighted(left: Any, left_weight: float, right: Any, right_weight: float) -> float:
    return (float(left or 0) * left_weight + float(right or 0) * right_weight) / max(left_weight + right_weight, 1)


def poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
