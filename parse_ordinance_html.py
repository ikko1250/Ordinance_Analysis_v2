#!/usr/bin/env python3
"""
市町村条例HTMLテーブルをパースしてデータベースに保存するスクリプト
"""

import sqlite3
import re
import csv
import unicodedata
from bs4 import BeautifulSoup
from typing import List, Dict, Tuple
import json

class OrdinanceParser:
    def __init__(
        self,
        html_file: str,
        db_file: str = "data/ordinance_data.db",
        municipality_list_file: str = "data/地方自治体リスト.csv",
    ):
        self.html_file = html_file
        self.db_file = db_file
        self.municipality_list_file = municipality_list_file
        self.conn = None
        self.cursor = None
        self._municipality_full_map = {}
        self._municipality_name_map = {}
        self._prefecture_name_map = {}
        self._prefecture_municipalities = {}
        self._prefecture_name_list = []
        self._load_municipality_list()

    def connect_db(self):
        """データベース接続"""
        self.conn = sqlite3.connect(self.db_file)
        self.cursor = self.conn.cursor()

    def _normalize_municipality_text(self, text: str) -> str:
        """自治体名を検索用に正規化"""
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", "", text)
        return text.replace("ヶ", "ケ").replace("ヵ", "カ")

    def _load_municipality_list(self):
        """地方自治体リストを読み込んで検索用インデックスを作成"""
        with open(self.municipality_list_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                prefecture = (row.get("都道府県名（漢字）") or "").strip()
                municipality = (row.get("市区町村名（漢字）") or "").strip()
                if not prefecture:
                    continue

                normalized_prefecture = self._normalize_municipality_text(prefecture)
                self._prefecture_name_map.setdefault(normalized_prefecture, prefecture)

                if not municipality:
                    continue

                normalized_municipality = self._normalize_municipality_text(municipality)
                full_name = prefecture + municipality
                normalized_full_name = self._normalize_municipality_text(full_name)

                self._municipality_full_map.setdefault(
                    normalized_full_name, (prefecture, municipality)
                )
                self._municipality_name_map.setdefault(normalized_municipality, []).append(
                    (prefecture, municipality)
                )
                self._prefecture_municipalities.setdefault(prefecture, {})[
                    normalized_municipality
                ] = municipality

        self._prefecture_name_list = sorted(
            self._prefecture_name_map.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def create_tables(self):
        """テーブル作成"""
        # 自治体マスタ
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS municipalities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prefecture_name TEXT NOT NULL,
                municipality_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(prefecture_name, municipality_name)
            )
        """)

        # 条例テーブル
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS ordinances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                municipality_id INTEGER NOT NULL,
                ordinance_name TEXT NOT NULL,
                url TEXT,
                enactment_year TEXT NOT NULL,
                promulgation_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (municipality_id) REFERENCES municipalities(id)
            )
        """)

        # 施行日テーブル
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS implementation_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ordinance_id INTEGER NOT NULL,
                implementation_date TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ordinance_id) REFERENCES ordinances(id)
            )
        """)

        try:
            self.cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ordinances_unique
                ON ordinances (municipality_id, ordinance_name, enactment_year)
            """)
            self.cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_implementation_dates_unique
                ON implementation_dates (ordinance_id, implementation_date, description)
            """)
        except sqlite3.IntegrityError as exc:
            raise sqlite3.IntegrityError(
                "Duplicate rows detected. Run document/database/dedupe_ordinances.sql before proceeding."
            ) from exc

        self.conn.commit()

    def parse_era_year_to_seireki(self, era: str, year: int) -> str:
        """和暦年度を西暦に変換"""
        era_base_year = {
            "平成": 1988,
            "令和": 2018
        }

        base_year = era_base_year.get(era)
        if base_year is None:
            return None
        return str(base_year + year)

    def parse_date_to_standard_format(self, date_str: str) -> str:
        """日付を標準フォーマットに変換"""
        if not date_str:
            return None

        # "平成26年1月29日公布" のような形式から日付部分を抽出
        date_match = re.search(r'(平成|令和)(\d+|元)年(\d+)月(\d+)日', date_str)
        if date_match:
            era = date_match.group(1)
            year = date_match.group(2)
            month = date_match.group(3)
            day = date_match.group(4)

            # 年号を西暦に変換
            if year == "元":
                year = 1
            else:
                year = int(year)

            seireki = self.parse_era_year_to_seireki(era, year)
            if not seireki:
                return date_str

            return f"{seireki}-{month.zfill(2)}-{day.zfill(2)}"

        return date_str

    def extract_municipality_info(self, municipality_text: str) -> Tuple[str, str]:
        """自治体名から都道府県と市区町村を分離"""
        normalized_text = self._normalize_municipality_text(municipality_text)

        # 1) 都道府県+市区町村の完全一致
        full_match = self._municipality_full_map.get(normalized_text)
        if full_match:
            return full_match

        # 2) 都道府県名が先頭にある場合は自治体リストで補正
        for normalized_prefecture, prefecture in self._prefecture_name_list:
            if normalized_text.startswith(normalized_prefecture):
                municipality_part = normalized_text[len(normalized_prefecture):]
                municipality_map = self._prefecture_municipalities.get(prefecture, {})
                if municipality_part:
                    municipality = municipality_map.get(municipality_part)
                    if municipality:
                        return prefecture, municipality

                # リストにない場合は元の文字列から分離
                if municipality_text.startswith(prefecture):
                    remainder = municipality_text[len(prefecture):]
                else:
                    remainder = municipality_text.replace(prefecture, "", 1)
                return prefecture, remainder or municipality_text

        # 3) 市区町村名のみの一致（政令指定都市など）
        municipality_matches = self._municipality_name_map.get(normalized_text)
        if municipality_matches:
            if len(municipality_matches) == 1:
                return municipality_matches[0]

        # 4) フォールバック: 正規表現で分離
        prefecture_pattern = r"(^(.+?都|.+?府|.+?道|.+?県))"
        match = re.search(prefecture_pattern, municipality_text)
        if match:
            prefecture = match.group(1)
            municipality = municipality_text.replace(prefecture, "", 1)
            return prefecture, municipality

        # 都道府県が見つからない場合（例：政令指定都市等）
        return "不明", municipality_text

    def parse_html(self) -> List[Dict]:
        """HTMLファイルをパース"""
        with open(self.html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()

        soup = BeautifulSoup(html_content, 'html.parser')

        ordinance_data = []
        current_enactment_year = None

        # 全てのpタグを探す
        all_p_tags = soup.find_all('p')

        for i, p_tag in enumerate(all_p_tags):
            p_text = p_tag.get_text(strip=True)

            # 年度見出しをチェック
            year_match = re.search(r'（(.+)制定）', p_text)
            if year_match:
                current_enactment_year = year_match.group(1)
                print(f"Found year: {current_enactment_year}")

                # 次の年度見出しが見つかるまでのすべてのテーブルを処理
                for j in range(i + 1, len(all_p_tags)):
                    next_p = all_p_tags[j]
                    next_text = next_p.get_text(strip=True)

                    # 次の年度見出しに達したら終了
                    if re.search(r'（(.+)制定）', next_text):
                        break

                    # 現在のpタグの中にtableがあるかチェック
                    table = next_p.find('table')
                    if table:
                        rows = table.find_all('tr')

                        for row in rows:  # 全ての行を処理（ヘッダーがないため）
                            cells = row.find_all('td')
                            if len(cells) >= 4:
                                # 自治体情報
                                municipality_text = cells[0].get_text(strip=True)
                                prefecture, municipality = self.extract_municipality_info(municipality_text)

                                # 条例名とURL
                                ordinance_name = ""
                                url = None
                                name_cell = cells[1]

                                # リンクを探す
                                link = name_cell.find('a')
                                if link:
                                    url = link.get('href', '')
                                    # テキストをすべて結合
                                    ordinance_name = name_cell.get_text(strip=True)
                                else:
                                    ordinance_name = name_cell.get_text(strip=True)

                                # 公布日
                                promulgation_text = cells[2].get_text(strip=True)
                                promulgation_date = self.parse_date_to_standard_format(promulgation_text)

                                # 施行日
                                implementation_dates = []
                                impl_cell = cells[3]

                                # セル内のテキスト（pタグ以外も含む）を処理
                                for impl_text in impl_cell.stripped_strings:
                                    impl_date = self.parse_date_to_standard_format(impl_text)
                                    description = "初回施行" if "改正" not in impl_text else "改正施行"
                                    implementation_dates.append({
                                        'date': impl_date,
                                        'description': description
                                    })

                                # データを保存
                                ordinance_data.append({
                                    'prefecture': prefecture,
                                    'municipality': municipality,
                                    'ordinance_name': ordinance_name,
                                    'url': url,
                                    'enactment_year': current_enactment_year,
                                    'promulgation_date': promulgation_date,
                                    'implementation_dates': implementation_dates
                                })
                                print(f"Processed: {prefecture} {municipality}")

        return ordinance_data

    def save_to_database(self, ordinance_data: List[Dict]):
        """データをデータベースに保存"""
        for data in ordinance_data:
            # 自治体を保存
            self.cursor.execute("""
                INSERT OR IGNORE INTO municipalities (prefecture_name, municipality_name)
                VALUES (?, ?)
            """, (data['prefecture'], data['municipality']))

            # 自治体IDを取得
            self.cursor.execute("""
                SELECT id FROM municipalities
                WHERE prefecture_name = ? AND municipality_name = ?
            """, (data['prefecture'], data['municipality']))

            municipality_id = self.cursor.fetchone()[0]

            # 条例を保存
            self.cursor.execute("""
                INSERT INTO ordinances
                (municipality_id, ordinance_name, url, enactment_year, promulgation_date)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(municipality_id, ordinance_name, enactment_year)
                DO UPDATE SET
                    url = COALESCE(NULLIF(ordinances.url, ''), excluded.url),
                    promulgation_date = COALESCE(NULLIF(ordinances.promulgation_date, ''), excluded.promulgation_date)
            """, (municipality_id, data['ordinance_name'], data['url'],
                  data['enactment_year'], data['promulgation_date']))

            self.cursor.execute("""
                SELECT id FROM ordinances
                WHERE municipality_id = ? AND ordinance_name = ? AND enactment_year = ?
            """, (municipality_id, data['ordinance_name'], data['enactment_year']))
            ordinance_id = self.cursor.fetchone()[0]

            # 施行日を保存
            for impl_data in data['implementation_dates']:
                self.cursor.execute("""
                    INSERT OR IGNORE INTO implementation_dates
                    (ordinance_id, implementation_date, description)
                    VALUES (?, ?, ?)
                """, (ordinance_id, impl_data['date'], impl_data['description']))

        self.conn.commit()

    def export_to_json(self, ordinance_data: List[Dict], output_file: str = "data/ordinance_data.json"):
        """JSONファイルに出力"""
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(ordinance_data, f, ensure_ascii=False, indent=2)

    def run(self):
        """メイン処理"""
        print("データベース接続...")
        self.connect_db()

        print("テーブル作成...")
        self.create_tables()

        print("HTMLパース開始...")
        ordinance_data = self.parse_html()
        print(f"{len(ordinance_data)}件の条例データを抽出しました")

        print("データベース保存開始...")
        self.save_to_database(ordinance_data)

        print("JSONエクスポート...")
        self.export_to_json(ordinance_data)

        print("完了!")

        # 統計情報を表示
        self.cursor.execute("SELECT COUNT(*) FROM municipalities")
        print(f"自治体数: {self.cursor.fetchone()[0]}")

        self.cursor.execute("SELECT COUNT(*) FROM ordinances")
        print(f"条例数: {self.cursor.fetchone()[0]}")

        self.cursor.execute("SELECT COUNT(*) FROM implementation_dates")
        print(f"施行日データ数: {self.cursor.fetchone()[0]}")

if __name__ == "__main__":
    parser = OrdinanceParser("asset/municipal_ordinance_tables.html")
    parser.run()
