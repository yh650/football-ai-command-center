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
        matches = balance_slate_decisions(current_rows, matches, profiles, team_profiles, intelligence)
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


def team_outcome_view(row: dict[str, Any], teams: dict[str, dict[str, Any]]) -> dict[str, Any]:
    home = teams.get(normalize_team(row.get("home"))) or {}
    away = teams.get(normalize_team(row.get("away"))) or {}
    home_sample = int(home.get("sample") or 0)
    away_sample = int(away.get("sample") or 0)
    if home and away:
        values = normalize({
            "home": (float(home.get("homeWinRate") or home.get("winRate") or 0.38) + float(away.get("awayLossRate") or away.get("lossRate") or 0.34)) / 2,
            "draw": (float(home.get("homeDrawRate") or home.get("drawRate") or 0.28) + float(away.get("awayDrawRate") or away.get("drawRate") or 0.28)) / 2,
            "away": (float(home.get("homeLossRate") or home.get("lossRate") or 0.34) + float(away.get("awayWinRate") or away.get("winRate") or 0.38)) / 2,
        })
        sample = home_sample + away_sample
        weight = 0.12 if sample >= 20 else (0.08 if sample >= 8 else (0.05 if sample >= 4 else 0.0))
        coverage = "双方球队画像"
    elif home:
        values = normalize({
            "home": float(home.get("homeWinRate") or home.get("winRate") or 0.40),
            "draw": float(home.get("homeDrawRate") or home.get("drawRate") or 0.28),
            "away": float(home.get("homeLossRate") or home.get("lossRate") or 0.32),
        })
        sample = home_sample
        weight = 0.06 if sample >= 16 else (0.04 if sample >= 8 else 0.0)
        coverage = "仅主队画像"
    elif away:
        values = normalize({
            "home": float(away.get("awayLossRate") or away.get("lossRate") or 0.34),
            "draw": float(away.get("awayDrawRate") or away.get("drawRate") or 0.28),
            "away": float(away.get("awayWinRate") or away.get("winRate") or 0.38),
        })
        sample = away_sample
        weight = 0.06 if sample >= 16 else (0.04 if sample >= 8 else 0.0)
        coverage = "仅客队画像"
    else:
        values, sample, weight, coverage = {}, 0, 0.0, "无球队画像"
    return {"probabilities": values, "sample": sample, "weight": weight, "coverage": coverage}


def competition_is_volatile(league: str, row: dict[str, Any] | None = None) -> bool:
    row = row or {}
    context = " ".join(str(row.get(key) or "") for key in ("stage", "phase", "competition", "round"))
    if any(token in f"{league} {context}" for token in ("资格", "附加", "淘汰", "两回合", "杯")):
        return True
    match_date = str(row.get("match_time") or row.get("date") or "")
    month_match = re.search(r"\d{4}-(\d{2})-\d{2}", match_date)
    summer_qualifier = bool(month_match and int(month_match.group(1)) in {6, 7, 8})
    return summer_qualifier and any(token in str(league) for token in ("欧冠", "欧罗巴", "欧协联"))


def balance_slate_decisions(
    rows: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    teams: dict[str, dict[str, Any]],
    intelligence: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    draw_picks = [
        (index, match) for index, match in enumerate(matches)
        if (match.get("conclusion") or {}).get("primary") == "平局"
        and (match.get("conclusion") or {}).get("decisionMode") == "bold-cold"
    ]
    draw_limit = max(1, round(len(matches) * 0.34)) if matches else 0
    if len(draw_picks) <= draw_limit:
        return matches

    ranked = sorted(
        draw_picks,
        key=lambda item: (
            float((((item[1].get("upset") or {}).get("roundtable") or {}).get("consensusScore") or 0)),
            int((((item[1].get("upset") or {}).get("roundtable") or {}).get("coreSupports") or 0)),
            float((item[1].get("probabilities") or {}).get("draw") or 0),
        ),
        reverse=True,
    )
    keep = {index for index, _ in ranked[:draw_limit]}
    rows_by_id = {str(row.get("id")): row for row in rows}
    output = list(matches)
    for index, match in draw_picks:
        if index in keep:
            continue
        original = rows_by_id.get(str(match.get("id")))
        if not original:
            continue
        rerun_row = {**original, "suppress_draw_primary": True}
        output[index] = build_match(
            rerun_row,
            profiles,
            teams,
            intelligence.get(str(original.get("id"))),
        )
    return output


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
    team_view = team_outcome_view(row, teams)
    team_probs = team_view.get("probabilities") or {}
    volatile = competition_is_volatile(league, row)
    market_weight = 0.68 if volatile else 0.76
    team_weight = float(team_view.get("weight") or 0)
    league_weight = 1 - market_weight - team_weight
    probs = normalize({
        "home": market["home"] * market_weight + profile["homeRate"] * league_weight + float(team_probs.get("home") or profile["homeRate"]) * team_weight,
        "draw": market["draw"] * market_weight + profile["drawRate"] * league_weight + float(team_probs.get("draw") or profile["drawRate"]) * team_weight,
        "away": market["away"] * market_weight + profile["awayRate"] * league_weight + float(team_probs.get("away") or profile["awayRate"]) * team_weight,
    })
    probs, intelligence_adjustment = apply_intelligence_adjustment(probs, intelligence)
    ranked = sorted(probs, key=probs.get, reverse=True)
    base_primary_key, base_second_key = ranked[0], ranked[1]
    base_gap = probs[base_primary_key] - probs[base_second_key]
    favorite_fail = 1 - float(profile.get("favoriteHitRate") or 0.55)
    favorite_odds = min(row["home_odds"], row["draw_odds"], row["away_odds"])
    cold_risk = clamp((1 - probs[base_primary_key]) * 0.72 + probs[base_second_key] * 0.22 + favorite_fail * 0.18, 0.18, 0.84)
    if favorite_odds >= 2.40:
        cold_risk = clamp(cold_risk + 0.07, 0, 0.88)
    if base_gap <= 0.05:
        cold_risk = clamp(cold_risk + 0.06, 0, 0.90)
    score_context = build_score_model_context(probs, profile, row, teams)
    base_draw_defense = local_draw_roundtable(
        probs, profile, row, teams, base_primary_key, base_second_key, base_gap, cold_risk, favorite_odds, score_context
    )
    upset_roundtable = local_upset_roundtable(
        probs, market, profile, row, teams, base_primary_key, base_second_key, cold_risk,
        score_context, base_draw_defense, team_view,
    )
    bold_pick = bool(upset_roundtable.get("bold"))
    primary_key = str(upset_roundtable.get("pickKey") or base_primary_key) if bold_pick else base_primary_key
    upset_key = base_primary_key if bold_pick else base_second_key
    primary, upset_direction = OUTCOME_LABELS[primary_key], OUTCOME_LABELS[upset_key]
    decision_gap = abs(probs[primary_key] - probs[upset_key])
    if bold_pick:
        cold_risk = max(cold_risk, 0.72 if upset_roundtable.get("level") == "deep" else 0.66)
        draw_defense = local_draw_roundtable(
            probs, profile, row, teams, primary_key, upset_key, decision_gap, cold_risk, favorite_odds, score_context
        )
        protection_key = base_primary_key
    else:
        draw_defense = base_draw_defense
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
    risk_level = "高" if cold_risk >= 0.66 else ("中" if cold_risk >= 0.52 else ("低中" if cold_risk >= 0.40 else "低"))
    anti_draw_value = draw_defense["antiScore"]
    anti_draw_verdict = draw_defense["verdict"]
    scores, cold_scores, expected_total, open_game = generate_scores(
        probs, profile, primary_key, upset_key, cold_risk, row, teams, protection_key, score_context
    )
    if bold_pick:
        cold_scores = []
    score_totals = [sum(int(part) for part in item["score"].split("-")) for item in scores]
    over_under = "大2.5球倾向" if open_game and all(total >= 3 for total in score_totals) else ("小2.5球倾向" if all(total <= 2 for total in score_totals) else "2/3球临界")
    reliability_bonus = 6 if profile.get("reliable") else 2
    if bold_pick:
        confidence = int(upset_roundtable.get("confidence") or 52)
    else:
        confidence = round(clamp(50 + base_gap * 70 + (1 - cold_risk) * 15 + reliability_bonus, 45, 90))
        if draw_defense["level"] == "must":
            confidence = min(confidence - 4, 82)
        elif draw_defense["cover"]:
            confidence = min(confidence - 2, 86)
    protection_direction = OUTCOME_LABELS[protection_key]
    cover = primary if protection_key == primary_key else f"{primary}，防{protection_direction}"
    if bold_pick:
        action = f"{'深冷' if upset_roundtable.get('level') == 'deep' else '冷门'}主判：{primary}"
        value_gate = "观点模式：接受高波动，冷门席拥有最终表决权"
    else:
        action = "重点候选" if confidence >= 72 and cold_risk < 0.60 else (f"防冷优先：{cover}" if cold_risk >= 0.66 else "观察复核")
        value_gate = "通过：进入候选观察池" if action == "重点候选" else ("高风险：只做防冷复盘" if cold_risk >= 0.66 else "观察：等待临场确认")
    if bold_pick:
        defend = f"主动冷门立场：{primary}；原热门仅作反向防守"
    elif protection_key == "draw" and primary_key != "draw":
        defend = "强制提示：防平局" if draw_defense["level"] == "must" else "建议保留：防平局"
    else:
        defend = f"强制提示：防{upset_direction}" if cold_risk >= 0.66 else (f"建议保留：防{upset_direction}" if cold_risk >= 0.52 else f"轻度观察：{upset_direction}")
    score_text = "、".join(item["score"] for item in scores)
    if bold_pick:
        final = f"本地冷门圆桌主动推翻赔率第一顺位，主判改为{primary}，观点信心{confidence}/100；执行口径为{cover}；"
    else:
        final = f"500竞彩赔率、联赛画像与球队样本综合倾向为{primary}，信心指数{confidence}/100；"
    if not bold_pick and protection_key == "draw" and primary_key != "draw":
        final += f"平局防守等级为{draw_defense['label']}，执行口径为{cover}；"
    elif not bold_pick and cold_risk >= 0.52:
        final += f"爆冷评分{cold_risk:.1%}，重点防{upset_direction}；"
    final += f"两个最得意比分为{score_text}，{over_under}。"
    if cold_scores:
        final += f" 大球或比赛失控时，爆冷比分留意{'、'.join(item['score'] for item in cold_scores)}。"
    customer_summary = build_customer_summary(
        row, probs, profile, primary, protection_direction, cover, confidence, over_under, scores, cold_scores,
        intelligence, draw_defense, cold_risk, score_context, upset_roundtable,
    )

    odds = {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"]}
    agents = build_agents(
        row, profile, probs, cold_risk, risk_level, anti_draw_value, anti_draw_verdict, scores, cold_scores,
        confidence, cover, intelligence, draw_defense, upset_roundtable, primary,
    )
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
        "modelBlend": {"marketWeight": market_weight, "leagueWeight": league_weight, "teamWeight": team_weight, "teamCoverage": team_view.get("coverage")},
        "intelligence": intelligence,
        "intelligenceAdjustment": intelligence_adjustment,
        "grid": {"signal": "500 SP基线", "roi": None, "sample": profile.get("sample"), "bucket": "JCToday/Live"},
        "upset": {
            "score": cold_risk, "level": risk_level, "direction": primary if bold_pick else upset_direction,
            "selectedAsPrimary": bold_pick, "counterDirection": upset_direction if bold_pick else OUTCOME_LABELS[base_primary_key],
            "trap": f"热门赔率{favorite_odds:.2f}；基础方向领先{base_gap:.1%}；联赛热门失手率{favorite_fail:.1%}",
            "reasons": upset_roundtable.get("reasons") if bold_pick else [f"综合非主方向概率{1-probs[base_primary_key]:.1%}", f"第二方向{OUTCOME_LABELS[base_second_key]}{probs[base_second_key]:.1%}", f"联赛样本{int(profile.get('sample') or 0)}场"],
            "roundtable": upset_roundtable,
        },
        "antiDraw": {
            "score": anti_draw_value, "verdict": anti_draw_verdict,
            "action": draw_defense["action"],
            "reasons": draw_defense["reasons"],
            "roundtable": {
                "consensusScore": draw_defense["consensusScore"],
                "coreSupports": draw_defense["coreSupports"],
                "supportAgents": draw_defense["supportAgents"],
                "opposeAgents": draw_defense["opposeAgents"],
                "bookmakerRole": "赔率公司仅作参考票，不能单独触发防平",
            },
        },
        "conclusion": {
            "action": action, "primary": primary, "cover": cover, "confidence": confidence, "valueGate": value_gate,
            "decisionMode": "deep-cold" if upset_roundtable.get("level") == "deep" else ("bold-cold" if bold_pick else "base"),
            "marketPrimary": OUTCOME_LABELS[base_primary_key],
            "bestScores": scores, "coldScores": cold_scores, "overUnder": over_under, "openGame": open_game,
            "defendCold": defend, "finalText": final, "customerSummary": customer_summary,
            "riskNotice": f"本场主动选择{primary}作为冷门主判，原热门{protection_direction}只作反向防守。" if bold_pick else f"爆冷可能性{risk_level}，当前执行防守方向为{protection_direction}。",
        },
        "agents": agents,
    }


def build_agents(row: dict[str, Any], profile: dict[str, Any], probs: dict[str, float], cold: float, level: str,
                 anti: int, anti_verdict: str, scores: list[dict[str, Any]], cold_scores: list[dict[str, Any]],
                 confidence: int, cover: str, intelligence: dict[str, Any],
                 draw_roundtable: dict[str, Any], upset_roundtable: dict[str, Any], final_primary: str) -> list[dict[str, Any]]:
    market_primary = OUTCOME_LABELS[max(probs, key=probs.get)]
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
        agent("Interwetten赔率Agent", "参考席", 64, f"即时赔率{row['home_odds']}/{row['draw_odds']}/{row['away_odds']}，只提供一张参考票，不单独决定防平或爆冷方向。"),
        agent("爆冷防线Agent", level, round(cold * 100), f"爆冷评分{cold:.1%}，第二方向为{OUTCOME_LABELS[sorted(probs, key=probs.get, reverse=True)[1]]}。"),
        agent(
            "冷门主张Agent", upset_roundtable.get("verdict") or "维持基础主线", int(upset_roundtable.get("confidence") or 50),
            f"核心支持{upset_roundtable.get('coreSupports', 0)}席；支持：{'、'.join(upset_roundtable.get('supportAgents') or []) or '无'}；"
            f"反对：{'、'.join(upset_roundtable.get('opposeAgents') or []) or '无'}。{'已推翻市场第一顺位' if upset_roundtable.get('bold') else '未达到推翻热门门槛'}。",
        ),
        agent(
            "本地防平圆桌Agent", anti_verdict, draw_roundtable["roundtableConfidence"],
            f"核心支持{draw_roundtable['coreSupports']}席；支持：{'、'.join(draw_roundtable['supportAgents']) or '无'}；"
            f"反对：{'、'.join(draw_roundtable['opposeAgents']) or '无'}。赔率公司只作参考。",
        ),
        agent("比分脚本Agent", score_signal, confidence, "常规比分与大球爆冷比分分层输出，由方向概率、进球基线、阵容影响和Poisson矩阵联合筛选。"),
        agent("圆桌仲裁Agent", cover, confidence, f"多Agent完成统一仲裁，基础热门{market_primary}，最终主方向{final_primary}。"),
    ]


def agent(name: str, signal: str, score: int, view: str) -> dict[str, Any]:
    return {"name": name, "status": "已执行", "signal": signal, "score": score, "view": view}


def anti_draw_score(probs: dict[str, float], profile: dict[str, Any], gap: float, cold: float, upset_key: str) -> int:
    score = 48 + (0.255 - probs["draw"]) * 120 + max(0.0, gap - 0.20) * 55 - cold * 14
    score += (0.255 - float(profile.get("drawRate") or 0.255)) * 40
    if upset_key == "draw":
        score -= 16
    return round(clamp(score, 0, 100))


def local_upset_roundtable(
    probs: dict[str, float],
    market: dict[str, float],
    profile: dict[str, Any],
    row: dict[str, Any],
    teams: dict[str, dict[str, Any]],
    primary_key: str,
    second_key: str,
    cold: float,
    score_context: dict[str, Any],
    draw_roundtable: dict[str, Any],
    team_view: dict[str, Any],
) -> dict[str, Any]:
    league = str(row.get("league") or "")
    favorite_key = min(("home", "draw", "away"), key=lambda key: float(row[f"{key}_odds"]))
    favorite_odds = float(row[f"{favorite_key}_odds"])
    primary_prob = float(probs[primary_key])
    favorite_hit = float(profile.get("favoriteHitRate") or 0.55)
    outcome_mass = score_context.get("outcomeMass") or {}
    volatile = competition_is_volatile(league, row)

    if volatile and primary_key == favorite_key and primary_key != "draw" and favorite_odds <= 1.42:
        opposite_key = "away" if primary_key == "home" else "home"
        opposite_odds = float(row[f"{opposite_key}_odds"])
        opposite_edge = float(probs[opposite_key]) - float(market[opposite_key])
        if opposite_odds >= 5.50 and float(probs[opposite_key]) >= 0.12 and opposite_edge >= 0.04:
            support_agents = ["赛制波动Agent", "热门过热Agent"]
            reasons = [
                f"{league}属于高波动淘汰/资格阶段",
                f"热门赔率低至{favorite_odds:.2f}，对面赔率达到{opposite_odds:.2f}",
                f"基础概率仍给{OUTCOME_LABELS[opposite_key]}保留{probs[opposite_key]:.1%}",
                f"联赛与球队模型把反向结果较市场隐含概率抬高{opposite_edge:.1%}",
                "极端盘不再只做口头防冷，冷门席获得正式主张权",
            ]
            return {
                "bold": True, "level": "deep", "verdict": f"深冷主判：{OUTCOME_LABELS[opposite_key]}",
                "pickKey": opposite_key, "marketPrimaryKey": primary_key, "candidateKey": opposite_key,
                "coreSupports": 2, "supportAgents": support_agents, "opposeAgents": ["基础概率Agent", "赔率参考Agent"],
                "consensusScore": 2.85, "confidence": round(clamp(48 + (1.42 - favorite_odds) * 18, 48, 55)),
                "reasons": reasons, "bookmakerRole": "极端低赔在资格赛中被视为过热证据，不拥有否决权",
            }

    draw_core = int(draw_roundtable.get("coreSupports") or 0)
    draw_prob = float(probs.get("draw") or 0)
    draw_bold = not row.get("suppress_draw_primary") and primary_key != "draw" and draw_core >= 2 and cold >= 0.47 and (
        primary_prob <= 0.525 or (favorite_hit <= 0.50 and primary_prob <= 0.57 and draw_prob >= 0.245)
    )
    if draw_bold:
        score = float(draw_roundtable.get("consensusScore") or 0) + max(0.0, 0.52 - favorite_hit) * 4
        return {
            "bold": True, "level": "bold", "verdict": "冷门主判：平局",
            "pickKey": "draw", "marketPrimaryKey": primary_key, "candidateKey": "draw",
            "coreSupports": draw_core, "supportAgents": draw_roundtable.get("supportAgents") or [],
            "opposeAgents": draw_roundtable.get("opposeAgents") or [], "consensusScore": round(score, 2),
            "confidence": round(clamp(49 + draw_core * 4 + max(0.0, 0.52 - favorite_hit) * 35, 50, 64)),
            "reasons": [
                f"平局得到{draw_core}个本地核心席独立支持",
                f"主方向基础概率只有{primary_prob:.1%}，没有形成压倒性优势",
                f"联赛热门命中率{favorite_hit:.1%}，冷门席拒绝继续只做观察",
            ],
            "bookmakerRole": "平赔只作参考，平局升为主判来自本地核心席共识",
        }

    candidates: list[dict[str, Any]] = []
    team_probs = team_view.get("probabilities") or {}
    for candidate_key in ("home", "draw", "away"):
        if candidate_key == primary_key or candidate_key == "draw":
            continue
        candidate_prob = float(probs[candidate_key])
        gap = primary_prob - candidate_prob
        consensus = 0.0
        core_supports: list[str] = []
        support_agents: list[str] = []
        oppose_agents: list[str] = []
        reasons = [f"基础概率{candidate_prob:.1%}", f"与热门差距{gap:.1%}"]
        if candidate_prob >= 0.28 and gap <= 0.17:
            consensus += 1.0
            core_supports.append("概率分歧Agent")
            support_agents.append("概率分歧Agent")
        elif candidate_prob >= 0.23 and gap <= 0.24:
            consensus += 0.65
            core_supports.append("概率分歧Agent")
            support_agents.append("概率分歧Agent")
        else:
            oppose_agents.append("概率分歧Agent")

        if team_probs:
            team_candidate = float(team_probs.get(candidate_key) or 0)
            team_primary = float(team_probs.get(primary_key) or 0)
            if team_candidate >= 0.42 or team_candidate >= team_primary + 0.06:
                consensus += 1.05
                core_supports.append("球队反向Agent")
                support_agents.append("球队反向Agent")
                reasons.append(f"{team_view.get('coverage')}给反向结果{team_candidate:.1%}")
            elif team_primary >= team_candidate + 0.18:
                consensus -= 0.55
                oppose_agents.append("球队反向Agent")

        candidate_mass = float(outcome_mass.get(candidate_key) or 0)
        primary_mass = float(outcome_mass.get(primary_key) or 0)
        if candidate_mass >= 0.27 and primary_mass - candidate_mass <= 0.16:
            consensus += 0.85
            core_supports.append("比分反转Agent")
            support_agents.append("比分反转Agent")
            reasons.append(f"比分矩阵反向结果质量{candidate_mass:.1%}")
        elif primary_mass - candidate_mass >= 0.30:
            consensus -= 0.45
            oppose_agents.append("比分反转Agent")

        if favorite_hit <= 0.52 and primary_key == favorite_key:
            consensus += 0.55
            support_agents.append("联赛反热门Agent")
        if volatile:
            consensus += 0.35
            support_agents.append("赛制波动Agent")
        core_count = len(set(core_supports))
        candidates.append({
            "key": candidate_key, "score": consensus, "core": core_count,
            "support": list(dict.fromkeys(support_agents)), "oppose": list(dict.fromkeys(oppose_agents)),
            "reasons": reasons,
        })

    best = max(candidates, key=lambda item: item["score"], default={"key": second_key, "score": 0.0, "core": 0, "support": [], "oppose": [], "reasons": []})
    bold = bool(best["core"] >= 2 and best["score"] >= 2.15 and cold >= 0.56 and float(probs[best["key"]]) >= 0.22)
    level = "bold" if bold else ("watch" if best["score"] >= 1.15 else "none")
    verdict = f"冷门主判：{OUTCOME_LABELS[best['key']]}" if bold else (f"冷门观察：{OUTCOME_LABELS[best['key']]}" if level == "watch" else "维持基础主线")
    if row.get("suppress_draw_primary"):
        best["reasons"] = ["全场次圆桌横向比较后，本场平局证据降为观察", *best["reasons"]]
    return {
        "bold": bold, "level": level, "verdict": verdict,
        "pickKey": best["key"] if bold else primary_key, "marketPrimaryKey": primary_key, "candidateKey": best["key"],
        "coreSupports": best["core"], "supportAgents": best["support"], "opposeAgents": best["oppose"],
        "consensusScore": round(float(best["score"]), 2),
        "confidence": round(clamp(48 + float(best["score"]) * 5 + int(best["core"]) * 2, 48, 64)),
        "reasons": best["reasons"][:5], "bookmakerRole": "赔率公司只能提供热门基线，不能否决独立冷门共识",
    }


def local_draw_roundtable(
    probs: dict[str, float],
    profile: dict[str, Any],
    row: dict[str, Any],
    teams: dict[str, dict[str, Any]],
    primary_key: str,
    upset_key: str,
    gap: float,
    cold: float,
    favorite_odds: float | None,
    score_context: dict[str, Any],
) -> dict[str, Any]:
    draw_prob = float(probs.get("draw") or 0)
    league_draw = float(profile.get("drawRate") or 0.255)
    primary_prob = float(probs.get(primary_key) or 0)
    draw_is_second = upset_key == "draw"
    consensus_score = 0.0
    core_supports: list[str] = []
    support_agents: list[str] = []
    oppose_agents: list[str] = []
    reasons = [f"模型平局概率{draw_prob:.1%}", f"联赛平局基准{league_draw:.1%}", f"主方向领先{gap:.1%}"]

    if draw_prob >= 0.285:
        consensus_score += 1.25
        core_supports.append("概率模型Agent")
        support_agents.append("概率模型Agent")
    elif draw_is_second and draw_prob >= 0.245:
        consensus_score += 1.0
        core_supports.append("概率模型Agent")
        support_agents.append("概率模型Agent")
    elif draw_is_second and draw_prob >= 0.225 and gap <= 0.22:
        consensus_score += 0.65
        core_supports.append("概率模型Agent")
        support_agents.append("概率模型Agent")
    elif draw_prob < 0.22:
        consensus_score -= 0.8
        oppose_agents.append("概率模型Agent")

    home_profile = teams.get(normalize_team(row.get("home"))) or {}
    away_profile = teams.get(normalize_team(row.get("away"))) or {}
    home_sample = int(home_profile.get("sample") or 0)
    away_sample = int(away_profile.get("sample") or 0)
    if home_sample >= 6 and away_sample >= 6:
        home_draw = float(home_profile.get("homeDrawRate") or home_profile.get("drawRate") or 0)
        away_draw = float(away_profile.get("awayDrawRate") or away_profile.get("drawRate") or 0)
        team_draw = (home_draw + away_draw) / 2
        if team_draw >= 0.29:
            consensus_score += 1.15
            core_supports.append("球队状态Agent")
            support_agents.append("球队状态Agent")
            reasons.append(f"主客场球队平局画像{team_draw:.1%}")
        elif team_draw <= 0.20:
            consensus_score -= 0.9
            oppose_agents.append("球队状态Agent")
    elif home_sample + away_sample >= 4 and home_profile and away_profile:
        home_draw = float(home_profile.get("homeDrawRate") or home_profile.get("drawRate") or 0)
        away_draw = float(away_profile.get("awayDrawRate") or away_profile.get("drawRate") or 0)
        team_draw = (home_draw + away_draw) / 2
        if team_draw >= 0.42:
            consensus_score += 0.65
            core_supports.append("球队状态Agent")
            support_agents.append("球队状态Agent")
            reasons.append(f"小样本主客场胶着率{team_draw:.1%}，降权后仍支持平局")

    draw_mass = float(score_context.get("drawMass") or 0)
    expected_total = float(score_context.get("expectedTotal") or 0)
    if draw_mass >= 0.285 and expected_total <= 2.55:
        consensus_score += 1.1
        core_supports.append("比分结构Agent")
        support_agents.append("比分结构Agent")
    elif draw_mass >= 0.225 and expected_total <= 2.55:
        consensus_score += 0.75
        core_supports.append("比分结构Agent")
        support_agents.append("比分结构Agent")
    elif draw_mass >= 0.215 and expected_total <= 2.45:
        consensus_score += 0.55
        core_supports.append("比分结构Agent")
        support_agents.append("比分结构Agent")
    elif expected_total >= 2.80 and draw_mass < 0.23:
        consensus_score -= 0.75
        oppose_agents.append("比分结构Agent")

    if league_draw >= 0.30:
        consensus_score += 0.45
        support_agents.append("联赛画像Agent")
    elif league_draw <= 0.23:
        consensus_score -= 0.45
        oppose_agents.append("联赛画像Agent")

    market = implied_probabilities(row["home_odds"], row["draw_odds"], row["away_odds"])
    if market["draw"] >= 0.285 or float(row["draw_odds"]) <= 3.25:
        consensus_score += 0.55
        support_agents.append("赔率参考Agent")
    elif float(row["draw_odds"]) >= 3.90:
        consensus_score -= 0.45
        oppose_agents.append("赔率参考Agent")

    if primary_prob >= 0.60 and gap >= 0.30:
        consensus_score -= 1.35
        oppose_agents.append("强弱差Agent")
    elif primary_prob >= 0.54 and gap >= 0.22:
        consensus_score -= 0.65
        oppose_agents.append("强弱差Agent")
    if row.get("home_rank") and row.get("away_rank") and abs(int(row["home_rank"]) - int(row["away_rank"])) >= 9:
        consensus_score -= 0.35
        oppose_agents.append("排名差Agent")

    core_count = len(set(core_supports))
    must_cover = primary_key != "draw" and core_count >= 2 and consensus_score >= 2.70 and draw_prob >= 0.28
    should_cover = primary_key != "draw" and core_count >= 2 and consensus_score >= 1.55
    should_watch = primary_key != "draw" and not should_cover and (consensus_score >= 0.55 or (draw_is_second and draw_prob >= 0.235))

    if primary_key == "draw":
        level, label, verdict, action = "primary", "平局主方向", "平局为主方向", "平局进入主判断层"
    elif must_cover:
        level, label, verdict, action = "must", "必须防平", "必须防平", "平局进入正式防守层"
        reasons.append(f"本地核心Agent有{core_count}席独立支持")
    elif should_cover:
        level, label, verdict, action = "suggest", "建议防平", "建议防平", "主方向 + 平局保护"
        reasons.append(f"本地核心Agent有{core_count}席独立支持")
    elif should_watch:
        level, label, verdict, action = "watch", "平局观察", "平局观察", "平局只进入风险提示"
        reasons.append("尚未达到两席核心Agent共同支持的正式防平门槛")
    else:
        level, label, verdict, action = "none", "有依据不防平", "有依据不防平", "单方向成立"
        reasons.append("本地核心Agent未形成防平共识，赔率参考票不单独生效")
    anti_score = round(clamp(58 - consensus_score * 14 - core_count * 4, 5, 92))
    roundtable_confidence = round(clamp(54 + abs(consensus_score) * 9 + core_count * 5, 50, 90))
    return {
        "cover": bool(should_cover or must_cover),
        "level": level,
        "label": label,
        "verdict": verdict,
        "action": action,
        "reasons": reasons[:5],
        "antiScore": anti_score,
        "consensusScore": round(consensus_score, 2),
        "coreSupports": core_count,
        "supportAgents": list(dict.fromkeys(support_agents)),
        "opposeAgents": list(dict.fromkeys(oppose_agents)),
        "roundtableConfidence": roundtable_confidence,
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
    profile: dict[str, Any],
    primary: str,
    secondary_direction: str,
    cover: str,
    confidence: int,
    over_under: str,
    scores: list[dict[str, Any]],
    cold_scores: list[dict[str, Any]],
    intelligence: dict[str, Any],
    draw_defense: dict[str, Any],
    cold_risk: float,
    score_context: dict[str, Any],
    upset_roundtable: dict[str, Any],
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
    narrative = build_local_summary_narrative(
        row, probs, profile, primary, secondary_direction, cover, confidence, over_under, score_line,
        intelligence, draw_defense, cold_risk, score_context, upset_roundtable,
    )
    return {
        "headline": narrative["headline"],
        "primary": primary,
        "secondary": secondary_direction,
        "cover": cover,
        "confidence": confidence,
        "overUnder": over_under,
        "mainScores": score_line,
        "coldScores": cold_line,
        "probabilityLine": f"主胜{probs['home']:.1%} · 平局{probs['draw']:.1%} · 客胜{probs['away']:.1%}",
        "decisionMode": "deep-cold" if upset_roundtable.get("level") == "deep" else ("bold-cold" if upset_roundtable.get("bold") else "base"),
        "analysis": narrative["text"],
    }


def build_local_summary_narrative(
    row: dict[str, Any],
    probs: dict[str, float],
    profile: dict[str, Any],
    primary: str,
    secondary: str,
    cover: str,
    confidence: int,
    over_under: str,
    score_line: str,
    intelligence: dict[str, Any],
    draw_roundtable: dict[str, Any],
    cold_risk: float,
    score_context: dict[str, Any],
    upset_roundtable: dict[str, Any],
) -> dict[str, str]:
    key = f"{row.get('home')}|{row.get('away')}|{row.get('match_time')}"
    variant = sum(ord(char) for char in key) % 3
    ranked = sorted(probs, key=probs.get, reverse=True)
    gap = probs[ranked[0]] - probs[ranked[1]]
    primary_prob = probs[ranked[0]]
    bold_pick = bool(upset_roundtable.get("bold"))
    market_primary = OUTCOME_LABELS.get(str(upset_roundtable.get("marketPrimaryKey")), OUTCOME_LABELS[ranked[0]])
    home = str(row.get("home") or "主队")
    away = str(row.get("away") or "客队")
    match_name = f"{home}对{away}"
    if primary == "主胜":
        direction_read = "主队更有条件掌握推进节奏，真正要验证的是领先后能否持续压住客队反击"
    elif primary == "客胜":
        direction_read = "客队的有效进攻和抗压结构更占优，主队的主场身份不足以单独扭转判断"
    else:
        direction_read = "双方都缺少持续拉开差距的证据，比赛更可能在反复试探中维持均衡"
    if bold_pick:
        openings = [
            f"这场不跟低赔走。基础概率仍把{market_primary}放在前面，但本地冷门圆桌最终把主判改成{primary}，这是主动观点，不是模糊防守。",
            f"先把态度亮出来：{match_name}选择{primary}作为最终立场。赔率第一顺位是{market_primary}，但它没有拿到圆桌否决权。",
            f"这场允许冷门席真正拍板。系统承认{market_primary}是市场热门，同时认为{primary}的赛制、结构和反转证据更值得冒险。",
        ]
    elif gap <= 0.08:
        openings = [
            f"先把结论摆出来：{match_name}并不是一眼能定性的比赛，几个方向贴得很近。{direction_read}。",
            f"{match_name}属于典型拉扯局，难点不是找赔率最低的一方，而是分辨哪种比赛脚本更容易落地。{direction_read}。",
            f"这场最需要防止的是被单一信号带着走。多席本地Agent重新拆分强弱、节奏和风险后，仍认为{direction_read}。",
        ]
    elif primary_prob >= 0.58:
        openings = [
            f"{match_name}的主线比较清楚，{primary}不是因为赔率更低才被选中，而是强弱、节奏和比分结构给出了同向证据。{direction_read}。",
            f"这场可以把态度说得明确一些：圆桌更信任{primary}。优势已经形成，但仍要区分“更可能发生”和“不会出意外”。",
            f"数据底座把{primary}推到明显领先位置，本地Agent随后又复核了第二风险。落到比赛内容上，{direction_read}。",
        ]
    else:
        openings = [
            f"{match_name}当前更偏向{primary}，但优势还没有大到可以把另一条比赛路线删掉。{direction_read}。",
            f"圆桌最后把{primary}放在第一顺位，这是各席证据交叉后的选择，不是照抄任何一家公司的推荐。",
            f"这场需要把主线和风险线分开看：主线仍是{primary}，同时承认比赛存在转折空间。{direction_read}。",
        ]

    support_agents = draw_roundtable.get("supportAgents") or []
    oppose_agents = draw_roundtable.get("opposeAgents") or []
    support = "、".join(support_agents) or "暂无明确支持席"
    oppose = "、".join(oppose_agents)
    upset_support = "、".join(upset_roundtable.get("supportAgents") or []) or "暂无明确支持席"
    upset_oppose = "、".join(upset_roundtable.get("opposeAgents") or []) or "没有形成强反对席"
    upset_text = ""
    if bold_pick:
        upset_text = (
            f"冷门主张得到{upset_roundtable.get('coreSupports', 0)}个核心席支持，支持方为{upset_support}；"
            f"反对方为{upset_oppose}。圆桌接受这是一笔高波动判断，因此错了也保留完整复盘轨迹，不把观点退回低赔正路。"
        )
    if bold_pick and primary == "平局":
        draw_text = (
            f"平局不再只是“防一下”，而是从风险层升为最终主判。基础平局概率{probs['draw']:.1%}，"
            f"但{upset_roundtable.get('coreSupports', 0)}个核心席认为热门优势不足以兑现。"
        )
    elif draw_roundtable.get("cover"):
        opposition_clause = f"反对方为{oppose}，但票数和证据不足以推翻保护。" if oppose else "圆桌没有出现足以否决防平的核心反对票。"
        draw_text = (
            f"圆桌在平局问题上不是顺着平赔走：{draw_roundtable.get('coreSupports', 0)}个本地核心席位给出独立支持，"
            f"支持方为{support}；{opposition_clause}因此平局才进入正式保护。"
        )
    elif draw_roundtable.get("level") == "watch":
        opposition_clause = f"反对方为{oppose}" if oppose else "没有强反对票"
        draw_text = (
            f"本地圆桌认为平局有一定讨论价值，但只有{draw_roundtable.get('coreSupports', 0)}个核心席位支持，未达到正式防平门槛。"
            f"支持方为{support}，{opposition_clause}，所以只保留观察，不机械塞进最终选择。"
            "Interwetten与500的平赔仍只是一张参考票。"
        )
    else:
        opposition_clause = f"反对方主要来自{oppose}" if oppose else "核心席位没有形成支持"
        draw_text = (
            f"平局没有形成两席以上本地核心共识，{opposition_clause}。"
            "Interwetten与500的平赔只记作参考票，不会单独把结论改成防平。"
        )

    expected_total = float(score_context.get("expectedTotal") or 0)
    profile_sample = int(profile.get("sample") or 0)
    sample_text = f"联赛历史画像{profile_sample}场" if profile_sample else "综合联赛基线"
    pace_text = (
        f"落到执行层，{sample_text}与Poisson比分矩阵把两个首选比分收敛到{score_line}；"
        f"预期总进球{expected_total:.2f}，对应{over_under}。最终口径为{cover}，"
        f"信心{confidence}/100，爆冷压力约{cold_risk:.1%}。"
    )
    intelligence_text = str(intelligence.get("impactSummary") or "阵容影响暂时有限")
    news = intelligence.get("news") or []
    if news:
        intelligence_text = f"阵容层面，{intelligence_text}；临场还要盯住：{news[0].get('title', '')}。"
    else:
        intelligence_text = f"阵容层面，{intelligence_text}；当前没有足够可靠的新增伤停消息，系统不虚构球员结论。"
    if bold_pick and upset_roundtable.get("level") == "deep":
        headlines = [f"深冷立场：{primary}，主动反对{market_primary}热门", f"不跟极端低赔，圆桌主判{primary}", f"资格赛深冷：{primary}获得最终表决权"]
    elif bold_pick:
        headlines = [f"冷门立场：{primary}升为最终主判", f"圆桌推翻{market_primary}，选择{primary}", f"这场不走正路，最终观点是{primary}"]
    elif draw_roundtable.get("cover"):
        headlines = [f"{primary}是主线，平局经圆桌表决进入保护", f"方向看{primary}，平局保护有独立证据", f"主线落在{primary}，防平不是跟随平赔"]
    elif cold_risk >= 0.58 and secondary != "平局":
        headlines = [f"{primary}暂居上风，真正防线放在{secondary}", f"主选{primary}，第二风险不是平局而是{secondary}", f"圆桌选择{primary}，防冷重点转向{secondary}"]
    elif draw_roundtable.get("level") == "watch":
        headlines = [f"主看{primary}，平局只观察不强塞", f"方向偏{primary}，平局票数未过门槛", f"{primary}居前，平局保留观察席"]
    elif primary_prob >= 0.58:
        headlines = [f"{primary}优势清楚，平局未获圆桌共识", f"圆桌明确选择{primary}，不机械附加平局", f"主线清晰指向{primary}，防平证据不足"]
    else:
        headlines = [f"圆桌偏向{primary}，保留临场复核", f"第一顺位是{primary}，风险线另行处理", f"综合证据更支持{primary}，仍需临场确认"]
    paragraphs = [openings[variant]]
    if upset_text:
        paragraphs.append(upset_text)
    paragraphs.extend((draw_text, pace_text, intelligence_text))
    return {"headline": headlines[variant], "text": "\n".join(paragraphs)}


def generate_scores(probs: dict[str, float], profile: dict[str, Any], primary: str, upset: str, cold: float,
                    row: dict[str, Any], teams: dict[str, dict[str, Any]],
                    protection_key: str | None = None,
                    score_context: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, bool]:
    score_context = score_context or build_score_model_context(probs, profile, row, teams)
    home_lambda = float(score_context["homeLambda"])
    away_lambda = float(score_context["awayLambda"])
    expected_total = float(score_context["expectedTotal"])
    open_game = bool(score_context["openGame"])
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


def build_score_model_context(
    probs: dict[str, float],
    profile: dict[str, Any],
    row: dict[str, Any],
    teams: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    home_team = teams.get(normalize_team(row["home"])) or {}
    away_team = teams.get(normalize_team(row["away"])) or {}
    over_rate = float(profile.get("over25Rate") or 0.50)
    total = 2.35 + (over_rate - 0.50) * 1.4
    if home_team and away_team:
        observed_total = (
            float(home_team.get("goalsFor") or 1.2) + float(home_team.get("goalsAgainst") or 1.2) +
            float(away_team.get("goalsFor") or 1.2) + float(away_team.get("goalsAgainst") or 1.2)
        ) / 2
        total = total * 0.65 + clamp(observed_total, 1.6, 3.8) * 0.35
    edge = clamp((probs["home"] - probs["away"]) * 1.35, -0.85, 0.85)
    home_lambda = clamp(total / 2 + 0.16 + edge, 0.25, 3.6)
    away_lambda = clamp(total - home_lambda, 0.20, 3.2)
    expected_total = home_lambda + away_lambda
    outcome_mass = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for home_goals in range(6):
        for away_goals in range(6):
            outcome = "home" if home_goals > away_goals else ("away" if home_goals < away_goals else "draw")
            outcome_mass[outcome] += poisson(home_goals, home_lambda) * poisson(away_goals, away_lambda)
    draw_mass = outcome_mass["draw"]
    return {
        "homeLambda": home_lambda,
        "awayLambda": away_lambda,
        "expectedTotal": expected_total,
        "drawMass": draw_mass,
        "outcomeMass": outcome_mass,
        "openGame": expected_total >= 2.72 or over_rate >= 0.61,
    }


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
        match_id = str(match.get("id"))
        duplicate_id = next(
            (
                item_id for item_id, item in archive.items()
                if str(item.get("date") or "")[:10] == str(match.get("date") or "")[:10]
                and str(item.get("round") or "") == str(match.get("round") or "")
                and normalize_team(item.get("home")) == normalize_team(match.get("home"))
                and normalize_team(item.get("away")) == normalize_team(match.get("away"))
            ),
            None,
        )
        match_is_provisional = bool(re.fullmatch(r"500-周.\d{3}", match_id))
        duplicate_is_provisional = bool(duplicate_id and re.fullmatch(r"500-周.\d{3}", duplicate_id))
        archive_id = duplicate_id if duplicate_id and match_is_provisional and not duplicate_is_provisional else match_id
        previous = archive.get(archive_id) or (archive.get(duplicate_id) if duplicate_id else {}) or {}
        if duplicate_id and duplicate_id != archive_id:
            archive.pop(duplicate_id, None)
        archive[archive_id] = {
            "id": archive_id,
            "date": match.get("date"),
            "round": match.get("round"),
            "league": match.get("league"),
            "home": match.get("home"),
            "away": match.get("away"),
            "predictedPrimary": conclusion.get("primary"),
            "marketPrimary": conclusion.get("marketPrimary"),
            "decisionMode": conclusion.get("decisionMode") or "base",
            "cover": conclusion.get("cover"),
            "confidence": conclusion.get("confidence"),
            "bestScores": conclusion.get("bestScores") or [],
            "coldScores": conclusion.get("coldScores") or [],
            "overUnder": conclusion.get("overUnder"),
            "upsetScore": (match.get("upset") or {}).get("score"),
            "upsetRoundtable": (match.get("upset") or {}).get("roundtable") or {},
            "customerSummary": conclusion.get("customerSummary") or {},
            "createdAt": previous.get("createdAt") or now_iso(),
            "updatedAt": now_iso(),
            "finalScore": previous.get("finalScore") or "",
            "actualOutcome": previous.get("actualOutcome") or "",
            "directionHit": previous.get("directionHit"),
        }

    finished_by_teams: dict[str, list[dict[str, Any]]] = defaultdict(list)
    finished_by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history:
        key = f"{normalize_team(row.get('home'))}|{normalize_team(row.get('away'))}"
        finished_by_teams[key].append(row)
        if row.get("round"):
            finished_by_round[str(row.get("round"))].append(row)
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
        if not candidates and item.get("round"):
            candidates = [
                row for row in finished_by_round.get(str(item.get("round"))) or []
                if canonical_league(row.get("league")) == canonical_league(item.get("league"))
            ]
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
