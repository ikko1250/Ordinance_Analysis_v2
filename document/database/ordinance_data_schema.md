# ordinance_data.db データ構造

本ドキュメントは `data/ordinance_data.db` のテーブル構成と関連を整理したものです。

## テーブル一覧

- `municipalities`
- `ordinances`
- `implementation_dates`

## municipalities

自治体マスタ。

| カラム名 | 型 | 制約 | 説明 |
| --- | --- | --- | --- |
| id | INTEGER | PRIMARY KEY, AUTOINCREMENT | 自治体ID |
| prefecture_name | TEXT | NOT NULL | 都道府県名 |
| municipality_name | TEXT | NOT NULL | 自治体名 |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 作成日時 |

- ユニーク制約: `prefecture_name`, `municipality_name` の組み合わせ

## ordinances

条例の基本情報。

| カラム名 | 型 | 制約 | 説明 |
| --- | --- | --- | --- |
| id | INTEGER | PRIMARY KEY, AUTOINCREMENT | 条例ID |
| municipality_id | INTEGER | NOT NULL, FOREIGN KEY | 自治体ID (`municipalities.id`) |
| ordinance_name | TEXT | NOT NULL | 条例名 |
| url | TEXT |  | 条例の参照URL |
| enactment_year | TEXT | NOT NULL | 制定年 |
| promulgation_date | TEXT |  | 公布日 |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 作成日時 |

- ユニーク制約: `municipality_id`, `ordinance_name`, `enactment_year` の組み合わせ

## implementation_dates

条例の施行日情報。

| カラム名 | 型 | 制約 | 説明 |
| --- | --- | --- | --- |
| id | INTEGER | PRIMARY KEY, AUTOINCREMENT | 施行日ID |
| ordinance_id | INTEGER | NOT NULL, FOREIGN KEY | 条例ID (`ordinances.id`) |
| implementation_date | TEXT | NOT NULL | 施行日 |
| description | TEXT |  | 施行日に関する補足 |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 作成日時 |

- ユニーク制約: `ordinance_id`, `implementation_date`, `description` の組み合わせ

## リレーション

- `municipalities` 1 --- n `ordinances`
- `ordinances` 1 --- n `implementation_dates`
