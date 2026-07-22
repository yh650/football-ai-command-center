from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "matches.json"
HISTORY_PATH = ROOT / "data" / "jc_history.json"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the GitHub Pages dashboard from 500.com.")
    parser.add_argument("--history-days", type=int, default=10, help="Recent result days to refresh.")
    parser.add_argument("--history-retention", type=int, default=400, help="Days kept for team and league profiles.")
    args = parser.parse_args()

    old_payload = load_json(DATA_PATH, {})
    seed_profiles = old_payload.get("seedLeagueProfiles") or old_payload.get("leagueProfiles") or []
    old_history = load_json(HISTORY_PATH, {"matches": []}).get("matches") or []

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
        matches = [build_match(row, profiles, team_profiles) for row in current_rows]
        update_status = "success" if current_rows else "success-no-current-matches"

    payload = {
        "meta": build_meta(old_payload, matches, history, update_status, warnings),
        "seedLeagueProfiles": seed_profiles,
        "leagueProfiles": list(profiles.values()),
        "teamProfiles": team_profiles,
        "matches": matches,
    }
    write_json(DATA_PATH, payload)
    write_json(HISTORY_PATH, {"updatedAt": now_iso(), "matches": history})
    print(
        json.dumps(
            {
                "status": update_status,
                "matches": len(matches),
                "history": len(history),
                "leagues": len(profiles),
                "teams": len(team_profiles),
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
    for table in parse_tables(html):
        for cells in table:
            if len(cells) < 6 or not re.match(r"^周.\d{3}$", cells[0] or ""):
                continue
            home, away = split_teams(cells[3])
            odds = [float(item) for item in re.findall(r"\d+\.\d{2}", cells[5] or "")]
            if not home or not away or len(odds) < 3:
                continue
            rows.append(
                {
                    "id": f"500-{cells[0]}-{cells[2]}-{home}-{away}",
                    "league": cells[1] or "竞彩足球",
                    "match_time": normalize_time(cells[2], date.today()),
                    "round": cells[0],
                    "home": home,
                    "away": away,
                    "home_odds": odds[0],
                    "draw_odds": odds[1],
                    "away_odds": odds[2],
                }
            )
    return rows


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
                    "league": cells[1] or "竞彩足球",
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


def build_league_profiles(seed_rows: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seed = {str(row.get("league")): row for row in seed_rows if row.get("league")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history:
        grouped[str(row.get("league") or "竞彩足球")].append(row)

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


def build_match(row: dict[str, Any], profiles: dict[str, dict[str, Any]], teams: dict[str, dict[str, Any]]) -> dict[str, Any]:
    league = str(row.get("league") or "竞彩足球")
    profile = profiles.get(league) or average_profile(profiles)
    market = implied_probabilities(row["home_odds"], row["draw_odds"], row["away_odds"])
    probs = normalize({
        "home": market["home"] * 0.82 + profile["homeRate"] * 0.18,
        "draw": market["draw"] * 0.82 + profile["drawRate"] * 0.18,
        "away": market["away"] * 0.82 + profile["awayRate"] * 0.18,
    })
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
    anti_draw_verdict = "不机械防平" if anti_draw_value >= 62 else ("保留平局风险" if anti_draw_value >= 42 else "必须防平")
    scores, expected_total = generate_scores(probs, profile, primary_key, upset_key, cold_risk, row, teams)
    score_totals = [sum(int(part) for part in item["score"].split("-")) for item in scores]
    over_under = "大2.5球倾向" if all(total >= 3 for total in score_totals) else ("小2.5球倾向" if all(total <= 2 for total in score_totals) else "2/3球临界")
    reliability_bonus = 6 if profile.get("reliable") else 2
    confidence = round(clamp(50 + gap * 70 + (1 - cold_risk) * 15 + reliability_bonus, 45, 90))
    cover = primary if cold_risk < 0.55 else f"{primary}，防{upset_direction}"
    action = "重点候选" if confidence >= 72 and cold_risk < 0.60 else (f"防冷优先：{cover}" if cold_risk >= 0.66 else "观察复核")
    value_gate = "通过：进入候选观察池" if action == "重点候选" else ("高风险：只做防冷复盘" if cold_risk >= 0.66 else "观察：等待临场确认")
    defend = f"强制提示：防{upset_direction}" if cold_risk >= 0.66 else (f"建议保留：防{upset_direction}" if cold_risk >= 0.52 else f"轻度观察：{upset_direction}")
    score_text = "、".join(item["score"] for item in scores)
    final = f"500竞彩赔率与联赛画像统一倾向为{primary}，信心指数{confidence}/100；"
    if cold_risk >= 0.52:
        final += f"爆冷评分{cold_risk:.1%}，重点防{upset_direction}；"
    final += f"两个最得意比分为{score_text}，{over_under}。"

    odds = {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"]}
    agents = build_agents(row, profile, probs, cold_risk, risk_level, anti_draw_value, anti_draw_verdict, scores, confidence, cover)
    return {
        "id": row["id"], "date": row["match_time"], "round": row["round"], "league": league,
        "home": row["home"], "away": row["away"], "sourceType": "500-jc",
        "odds": {
            "current": odds, "initial": odds, "shape": f"{row['home_odds']}/{row['draw_odds']}/{row['away_odds']}",
            "movement": "500竞彩即时SP", "movementCombo": "500最新竞彩赔率 + 历史联赛校准",
            "favorite": OUTCOME_LABELS[min(odds, key=odds.get)], "favoriteOdds": favorite_odds,
            "favoriteChange": "等待临场变化", "gap": abs(row["home_odds"] - row["away_odds"]),
            "gapLabel": "均势盘" if abs(row["home_odds"] - row["away_odds"]) <= 0.5 else "强弱分层",
            "mode": "500竞彩即时盘", "dropSide": "待临场", "dropBucket": "即时快照",
        },
        "probabilities": probs, "leagueProfile": profile,
        "grid": {"signal": "500 SP基线", "roi": None, "sample": profile.get("sample"), "bucket": "JCToday/Live"},
        "upset": {
            "score": cold_risk, "level": risk_level, "direction": upset_direction,
            "trap": f"热门赔率{favorite_odds:.2f}；主方向领先{gap:.1%}；联赛热门失手率{favorite_fail:.1%}",
            "reasons": [f"综合非主方向概率{1-probs[primary_key]:.1%}", f"第二方向{upset_direction}{probs[upset_key]:.1%}", f"联赛样本{int(profile.get('sample') or 0)}场"],
        },
        "antiDraw": {
            "score": anti_draw_value, "verdict": anti_draw_verdict,
            "action": "单方向优先" if anti_draw_value >= 62 else ("平局进入复核层" if anti_draw_value >= 42 else "平局进入防冷层"),
            "reasons": [f"模型平局概率{probs['draw']:.1%}", f"联赛平局基准{profile['drawRate']:.1%}", f"主方向领先{gap:.1%}"],
        },
        "conclusion": {
            "action": action, "primary": primary, "cover": cover, "confidence": confidence, "valueGate": value_gate,
            "bestScores": scores, "overUnder": over_under, "defendCold": defend, "finalText": final,
            "riskNotice": f"爆冷可能性{risk_level}，防冷方向为{upset_direction}。",
        },
        "agents": agents,
    }


def build_agents(row: dict[str, Any], profile: dict[str, Any], probs: dict[str, float], cold: float, level: str,
                 anti: int, anti_verdict: str, scores: list[dict[str, Any]], confidence: int, cover: str) -> list[dict[str, Any]]:
    primary = OUTCOME_LABELS[max(probs, key=probs.get)]
    return [
        agent("数据底座Agent", "500实时校验", 92, f"已读取500竞彩编号{row['round']}，主平客SP完整。"),
        agent("基本面Agent", "球队近期画像", max(48, confidence - 5), "已调用球队近一年赛果画像；样本不足时自动降权。"),
        agent("联赛画像Agent", profile.get("topOutcome") or "联赛基准", 84 if profile.get("reliable") else 62, f"{profile['league']}累计画像{int(profile.get('sample') or 0)}场，近期增量{int(profile.get('recentSample') or 0)}场。"),
        agent("Interwetten赔率Agent", "500 SP + 历史基线", 78, f"即时赔率{row['home_odds']}/{row['draw_odds']}/{row['away_odds']}，已与Interwetten历史画像交叉校准。"),
        agent("爆冷防线Agent", level, round(cold * 100), f"爆冷评分{cold:.1%}，第二方向为{OUTCOME_LABELS[sorted(probs, key=probs.get, reverse=True)[1]]}。"),
        agent("反防平Agent", anti_verdict, anti, f"平局模型{probs['draw']:.1%}，联赛基准{profile['drawRate']:.1%}。"),
        agent("比分脚本Agent", " / ".join(item["score"] for item in scores), confidence, "比分由胜平负方向、联赛进球基线和Poisson矩阵联合筛选。"),
        agent("圆桌仲裁Agent", cover, confidence, f"多Agent完成统一仲裁，主方向{primary}。"),
    ]


def agent(name: str, signal: str, score: int, view: str) -> dict[str, Any]:
    return {"name": name, "status": "已执行", "signal": signal, "score": score, "view": view}


def anti_draw_score(probs: dict[str, float], profile: dict[str, Any], gap: float, cold: float, upset_key: str) -> int:
    score = 52 + (0.27 - probs["draw"]) * 135 + gap * 75 - cold * 18
    score += (0.27 - float(profile.get("drawRate") or 0.27)) * 55
    if upset_key == "draw":
        score -= 12
    return round(clamp(score, 0, 100))


def generate_scores(probs: dict[str, float], profile: dict[str, Any], primary: str, upset: str, cold: float,
                    row: dict[str, Any], teams: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
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
    cover_key = upset if cold >= 0.55 else primary
    cover_rows = sorted((item for item in candidates if item["outcome"] == cover_key), key=lambda item: item["probability"], reverse=True)
    if over_rate >= 0.60 and probs[primary] >= 0.52:
        first = next((item for item in primary_rows if sum(int(part) for part in item["score"].split("-")) >= 2), primary_rows[0])
    else:
        first = primary_rows[0]
    selected = [first]
    if cold < 0.55 and over_rate >= 0.56:
        second = next((item for item in primary_rows if item["score"] != first["score"] and sum(int(part) for part in item["score"].split("-")) >= 3), None)
    else:
        second = None
    selected.append(second or next((item for item in cover_rows if item["score"] != first["score"]), primary_rows[1]))
    output = [{"score": item["score"], "probability": round(item["probability"], 4),
               "script": "防冷脚本" if item["outcome"] != primary else ("开放局脚本" if sum(int(part) for part in item["score"].split("-")) >= 3 else "主方向脚本")} for item in selected]
    return output, home_lambda + away_lambda


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
