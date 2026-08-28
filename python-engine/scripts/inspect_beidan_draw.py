"""检查 spdex 库 BeidanDraw 是否有 08-25/08-26(issue 26087~26089) 开奖数据。"""

from __future__ import annotations

import pymysql

CONN = dict(
    host="10.23.94.209",
    port=33698,
    user="root",
    password="NHC4)_hhuV6!XzWu",
    database="spdex",
    charset="utf8mb4",
)


def main() -> None:
    conn = pymysql.connect(**CONN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT issue, matchno, beidan_id, home_name, away_name, "
                "handicap, score, result, result_des, spvalue, draw_datetime "
                "FROM BeidanDraw WHERE issue IN ('26087','26088','26089') "
                "ORDER BY issue, CAST(matchno AS UNSIGNED)"
            )
            rows = cur.fetchall()
        print(f"BeidanDraw 行数: {len(rows)}")
        for r in rows:
            print(" ", r)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT beidan_id, lota_id, home_name, away_name, goal_line "
                "FROM BeidanMatch WHERE beidan_id LIKE '26087\\_%' "
                "OR beidan_id LIKE '26088\\_%' OR beidan_id LIKE '26089\\_%' "
                "ORDER BY beidan_id"
            )
            mrows = cur.fetchall()
        print(f"BeidanMatch 行数: {len(mrows)}")
        for r in mrows[:60]:
            print(" ", r)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
