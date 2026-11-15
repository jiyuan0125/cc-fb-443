#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按内容方重传包回刷热度日表 / 周表 / 回刷日志 / 汇总。

被重传覆盖到的 (gid, day) 单元，按重传明细全量重算；未覆盖的日表行保持原值。
重复执行产物不变：backfill_log.csv 里记着回刷前的分数，重跑时沿用该分数作为
old_score（首次运行则取当前日表的值），因此不会因为日表已被覆盖而丢账。
"""

import csv
import os
import sys
from collections import Counter, OrderedDict
from datetime import date

DAILY = "heat_daily.csv"
WEEKLY = "heat_weekly.csv"
LOG = "backfill_log.csv"
SUMMARY = "heat_summary.txt"
WEIGHTS = "weights.csv"
RESEND_DIR = "resend"

DAILY_HEADER = ["gid", "day", "plays", "likes", "coins", "score"]
LOG_HEADER = ["gid", "day", "old_score", "new_score"]
WEEKLY_HEADER = ["gid", "week", "plays", "score"]

# 口径（同时写进 heat_summary.txt 的 notes 行）：
#  - 只统计审核态为 pass 的记录；pending / reject 一律不计入条数和分数。
#  - 同一记录号在重传包里出现多行时，以后出现的那行为准（后报的改过值）。
#  - 重传包是该内容该日的全量重报：日表该行按重传明细整体重算；
#    旧明细里有、重传包里没有的记录视为内容方已删除，不再计入。
#  - 同一观众同一内容同一天的多条 play 逐条计数，不做去重。
#  - 周表按自然周（周一至周日）聚合，week 列记该周周一日期。
NOTES = (
    "notes 仅统计审核态pass的记录,pending与reject不计入条数和分数;"
    "同一记录号多行时以后出现的行为准;"
    "重传包为全量重报,未出现的旧记录视为内容方删除;"
    "同一观众同一内容同一天多条play逐条计数不去重;"
    "days_touched为被重传覆盖的不同自然日数;"
    "周表按周一至周日的自然周聚合,week列记该周周一日期"
)

WEIGHT_DECIMALS = 1  # weights.csv 的权重保留一位小数


def round_half_up(value, ndigits=2):
    """十进制定点四舍五入，避免二进制浮点误差。"""
    scaled = value * (10 ** ndigits)
    return int(scaled + 0.5 + 1e-9) / (10 ** ndigits)


def read_weights(path):
    """返回 {type: 权重×10}，用整数累加避免浮点误差。"""
    weights = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            weights[row["type"].strip()] = int(
                round(float(row["weight"]) * 10 ** WEIGHT_DECIMALS)
            )
    return weights


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def parse_resend_name(filename):
    """g18_20261020.csv -> ('g18', '2026-10-20')"""
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    gid, ymd = stem.split("_")
    return gid, "%s-%s-%s" % (ymd[0:4], ymd[4:6], ymd[6:8])


def gid_key(gid):
    try:
        return (0, int(gid.lstrip("gG")))
    except ValueError:
        return (1, gid)


def recompute_cell(rows, weights):
    """按重传明细全量重算一个 (gid, day) 单元。"""
    latest = OrderedDict()
    for row in rows:
        latest[row["记录号"].strip()] = row  # 后出现的覆盖先出现的

    counts = Counter()
    score_tenths = 0
    for row in latest.values():
        if row["审核态"].strip() != "pass":
            continue
        rtype = row["类型"].strip()
        if rtype not in weights:
            continue
        counts[rtype] += 1
        score_tenths += weights[rtype]

    score = round_half_up(score_tenths / (10 ** WEIGHT_DECIMALS), 2)
    return counts["play"], counts["like"], counts["coin"], score


def load_original_scores(current_scores):
    """回刷前的分数：首次运行取当前日表，重跑时沿用已生成的 backfill_log。"""
    if not os.path.exists(LOG):
        return dict(current_scores)
    original = {}
    for row in read_csv_rows(LOG):
        original[(row["gid"], row["day"])] = row["old_score"]
    # 日志里只有变过的行，其余行回刷前后一致
    for key, score in current_scores.items():
        original.setdefault(key, score)
    return original


def main():
    base_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    os.chdir(base_dir)

    weights = read_weights(WEIGHTS)
    current_rows = read_csv_rows(DAILY)
    current_scores = {(r["gid"], r["day"]): "%.2f" % float(r["score"]) for r in current_rows}
    original_scores = load_original_scores(current_scores)

    # 收集被重传覆盖到的单元及其明细
    covered = {}
    resend_files = sorted(
        name for name in os.listdir(RESEND_DIR) if name.lower().endswith(".csv")
    )
    for name in resend_files:
        gid, day = parse_resend_name(name)
        covered[(gid, day)] = read_csv_rows(os.path.join(RESEND_DIR, name))

    # 重算被覆盖的行；未覆盖的行照旧保留
    cells = {}
    for row in current_rows:
        cells[(row["gid"], row["day"])] = [
            int(row["plays"]),
            int(row["likes"]),
            int(row["coins"]),
            "%.2f" % float(row["score"]),
        ]
    for (gid, day), rows in covered.items():
        plays, likes, coins, score = recompute_cell(rows, weights)
        cells[(gid, day)] = [plays, likes, coins, "%.2f" % score]

    ordered_keys = sorted(cells, key=lambda k: (gid_key(k[0]), k[1]))
    new_daily = [[gid, day] + cells[(gid, day)] for gid, day in ordered_keys]
    write_csv(DAILY, DAILY_HEADER, new_daily)

    # 回刷日志：分数相对回刷前发生变化的行，old_score 记回刷前的值
    log_rows = []
    for gid, day in ordered_keys:
        new_score = cells[(gid, day)][3]
        old_score = original_scores.get((gid, day), new_score)
        if "%.2f" % float(old_score) != new_score:
            log_rows.append([gid, day, "%.2f" % float(old_score), new_score])
    write_csv(LOG, LOG_HEADER, log_rows)

    # 自然周聚合：从刷新后的日表整表写出，分数按分累加后再取两位
    week_plays = {}
    week_cents = {}
    for gid, day, plays, _likes, _coins, score in new_daily:
        d = date.fromisoformat(day)
        week = date.fromordinal(d.toordinal() - d.weekday()).isoformat()
        key = (gid, week)
        week_plays[key] = week_plays.get(key, 0) + plays
        week_cents[key] = week_cents.get(key, 0) + int(round(float(score) * 100))
    weekly_rows = [
        [gid, week, week_plays[(gid, week)], "%.2f" % round_half_up(week_cents[(gid, week)] / 100.0, 2)]
        for gid, week in sorted(week_plays, key=lambda k: (gid_key(k[0]), k[1]))
    ]
    write_csv(WEEKLY, WEEKLY_HEADER, weekly_rows)

    # 汇总：四行，前三行名目与数值空格分隔，第四行为口径说明
    summary_lines = [
        "days_touched %d" % len({day for _, day in covered}),
        "rows_changed %d" % len(log_rows),
        "resends %d" % len(resend_files),
        NOTES,
    ]
    with open(SUMMARY, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(summary_lines) + "\n")


if __name__ == "__main__":
    main()
