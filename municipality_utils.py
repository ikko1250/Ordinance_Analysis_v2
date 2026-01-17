"""
条例名から自治体名を抽出し、例規集URLを取得するユーティリティモジュール
"""

import re
import json
from pathlib import Path
from typing import Optional, Dict, Any


class MunicipalityExtractor:
    """条例名から自治体名を抽出するクラス"""

    # 自治体名の終わりを示す接尾辞
    SUFFIXES = r'(都|道|府|県|市|区|町|村)'

    # よくある条例のキーワード（除外用）
    KEYWORDS = [
        '太陽光発電', '自然環境', '再生可能エネルギー', '風力発電',
        '建築物', '都市計画', '景観', '廃棄物', '環境',
        '中小企業', '観光', '文化財', '福祉', '子ども',
        '高齢者', '障害者', '男女共同参画', '平和', '安全',
        '防災', '消防', '交通安全', '公衆浴場', '墓地',
        '個人情報保護', '情報公開', '契約', '財産', '税',
        '平和都市', '暴力団排除', '空き家', '宿泊', '温泉'
    ]

    @classmethod
    def extract(cls, ordinance_name: str) -> Optional[str]:
        """
        条例名から自治体名を抽出する

        Args:
            ordinance_name: 条例名（例: "中川村太陽光発電施設の設置等に関する条例"）

        Returns:
            自治体名（例: "中川村"）。抽出できない場合はNone。

        Examples:
            >>> MunicipalityExtractor.extract("中川村太陽光発電施設の設置等に関する条例")
            '中川村'
            >>> MunicipalityExtractor.extract("瑞浪市における再生可能エネルギー発電設備の設置に関する条例")
            '瑞浪市'
        """
        # パターン1: <自治体名>における...
        pattern1 = r'^(.+?' + cls.SUFFIXES + r')における'
        match = re.search(pattern1, ordinance_name)
        if match:
            return match.group(1)

        # パターン2: <自治体名><キーワード>に関する条例
        # まず接尾辞で終わる最短のマッチを探す
        pattern2 = r'^(.+?' + cls.SUFFIXES + r')'
        match = re.search(pattern2, ordinance_name)
        if match:
            candidate = match.group(1)
            # キーワードで終わっていないことを確認
            for keyword in cls.KEYWORDS:
                if candidate.endswith(keyword):
                    break
            else:
                return candidate

        # パターン3: フォールバック - 先頭から接尾辞まで
        pattern3 = r'^([^あ-ん]{1,10}?' + cls.SUFFIXES + r')'
        match = re.search(pattern3, ordinance_name)
        if match:
            return match.group(1)

        return None

    @classmethod
    def extract_ordinance_keyword(cls, ordinance_name: str, municipality: str) -> str:
        """
        条例名から主要なキーワードを抽出する（検索用）

        Args:
            ordinance_name: 条例名
            municipality: 自治体名

        Returns:
            検索用キーワード

        Examples:
            >>> MunicipalityExtractor.extract_ordinance_keyword(
            ...     "中川村太陽光発電施設の設置等に関する条例", "中川村"
            ... )
            '太陽光発電施設の設置'
        """
        # 自治体名を除去
        text = ordinance_name.replace(municipality, '')

        # 「に関する条例」等の定型表現を除去
        for suffix in ['に関する条例', 'に関する規則', 'の施行に関する条例', '等に関する条例']:
            if suffix in text:
                text = text.split(suffix)[0]

        # 「における」を除去
        text = text.replace('における', '')

        return text.strip()


class MunicipalityURLMap:
    """自治体-URLマッピングを管理するクラス"""

    def __init__(self, map_file: str = 'asset/municipality_url_map.json'):
        """
        初期化

        Args:
            map_file: マッピングファイルのパス
        """
        self.map_file = Path(map_file)
        self._data = None
        self._load()

    def _load(self):
        """マッピングファイルを読み込む"""
        if not self.map_file.exists():
            raise FileNotFoundError(
                f"マッピングファイルが見つかりません: {self.map_file}\n"
                "asset/例規集採用自治体一覧.htmlから作成してください。"
            )

        with open(self.map_file, 'r', encoding='utf-8') as f:
            self._data = json.load(f)

    def get_url(self, municipality: str) -> Optional[Dict[str, Any]]:
        """
        自治体名から例規集URLを取得する

        Args:
            municipality: 自治体名

        Returns:
            {"prefecture": "都道府県名", "url": "例規集URL"} の辞書。
            見つからない場合はNone。

        Examples:
            >>> mapper = MunicipalityURLMap()
            >>> mapper.get_url("中川村")
            {'prefecture': '長野県', 'url': 'https://www1.g-reiki.net/vill.nakagawa.nagano/reiki_menu.html'}
        """
        return self._data.get(municipality)

    def search_by_keyword(self, keyword: str) -> list:
        """
        キーワードで自治体を検索する（部分一致）

        Args:
            keyword: 検索キーワード

        Returns:
            マッチした自治体のリスト [{"name": "自治体名", "prefecture": "都道府県", "url": "URL"}, ...]
        """
        results = []
        for municipality, data in self._data.items():
            if keyword in municipality:
                results.append({
                    "name": municipality,
                    "prefecture": data["prefecture"],
                    "url": data["url"]
                })
        return results

    def get_all_municipalities(self) -> list:
        """
        すべての自治体名を取得する

        Returns:
            自治体名のリスト
        """
        return list(self._data.keys())

    def get_statistics(self) -> Dict[str, int]:
        """
        統計情報を取得する

        Returns:
            {"total": 総数, "prefectures": 都道府県数, "by_type": {"市": 数, "町": 数, ...}}
        """
        from collections import Counter

        types = Counter()
        prefectures = set()

        for municipality, data in self._data.items():
            prefectures.add(data["prefecture"])
            # 接尾辞をカウント
            for suffix in ['都', '道', '府', '県', '市', '区', '町', '村']:
                if municipality.endswith(suffix):
                    types[suffix] += 1
                    break

        return {
            "total": len(self._data),
            "prefectures": len(prefectures),
            "by_type": dict(types)
        }


def generate_search_keywords(ordinance_name: str, municipality: str) -> list:
    """
    条例名から検索キーワードを生成する

    Args:
        ordinance_name: 条例名
        municipality: 自治体名

    Returns:
        検索キーワードのリスト（優先順位順）

    Examples:
        >>> generate_search_keywords(
        ...     "中川村太陽光発電施設の設置等に関する条例", "中川村"
        ... )
        ['太陽光発電施設 規則', '太陽光発電 施行規則', '太陽光 規則', '太陽光発電施設 規則']
    """
    extractor = MunicipalityExtractor()
    keyword = extractor.extract_ordinance_keyword(ordinance_name, municipality)

    # 検索キーワードのパターン
    patterns = [
        f"{keyword} 規則",
        f"{keyword} 施行規則",
        f"{keyword[:10]} 規則",  # 短縮版
        f"{keyword} 規則",  # 元のキーワード
    ]

    # 重複を除去
    seen = set()
    unique_patterns = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            unique_patterns.append(p)

    return unique_patterns


if __name__ == "__main__":
    # テストコード
    test_cases = [
        "中川村太陽光発電施設の設置等に関する条例",
        "御宿町自然環境等と再生可能エネルギー発電事業との調和に関する条例",
        "瑞浪市における再生可能エネルギー発電設備の設置と自然環境等の保全との調和に関する条例",
        "大津市太陽光発電設備の設置の規制等に関する条例",
        "焼津市自然環境等と再生可能エネルギー発電設備設置事業との調和に関する条例",
    ]

    print("=" * 60)
    print("条例名から自治体名抽出のテスト")
    print("=" * 60)

    for ordinance in test_cases:
        municipality = MunicipalityExtractor.extract(ordinance)
        status = "✓" if municipality else "✗"
        print(f"{status} {municipality or '抽出失敗'} <= {ordinance}")

        if municipality:
            keywords = generate_search_keywords(ordinance, municipality)
            print(f"  検索キーワード: {keywords[:3]}")
        print()

    print("=" * 60)
    print("マッピングファイルの統計")
    print("=" * 60)

    try:
        mapper = MunicipalityURLMap()
        stats = mapper.get_statistics()
        print(f"総自治体数: {stats['total']}")
        print(f"都道府県数: {stats['prefectures']}")
        print("種類別:")
        for k, v in sorted(stats['by_type'].items()):
            print(f"  {k}: {v}")

        # 特定の自治体を検索
        print("\n" + "=" * 60)
        print("特定の自治体のURL取得テスト")
        print("=" * 60)

        test_municipalities = ["中川村", "御宿町", "瑞浪市"]
        for m in test_municipalities:
            data = mapper.get_url(m)
            if data:
                print(f"✓ {m} ({data['prefecture']})")
                print(f"  URL: {data['url'][:70]}...")
            else:
                print(f"✗ {m}: 見つかりません")
            print()

    except FileNotFoundError as e:
        print(f"エラー: {e}")
