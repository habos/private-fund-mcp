-- SPEC section 4, applied by `etl.load` before the first chunk. Generated from etl/src/etl/schema.sql
-- (IF NOT EXISTS added so it can be re-run); tests check the two stay identical.
CREATE TABLE IF NOT EXISTS advisers (
  crd                 TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  name_norm           TEXT NOT NULL,
  status              TEXT,
  raum_usd            INTEGER,
  city                TEXT,
  state               TEXT,
  adv_filing_date     TEXT,
  fund_count          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_advisers_name_norm ON advisers(name_norm);

CREATE TABLE IF NOT EXISTS funds (
  pfid                TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  adviser_crd         TEXT NOT NULL REFERENCES advisers(crd),
  fund_type           TEXT NOT NULL,
  gross_asset_value_usd INTEGER,
  beneficial_owners   INTEGER,
  form_d_file_no      TEXT,
  auditor             TEXT,
  is_feeder           INTEGER NOT NULL DEFAULT 0,
  master_pfid         TEXT,
  adv_filing_date     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funds_adviser ON funds(adviser_crd);
CREATE INDEX IF NOT EXISTS idx_funds_file_no ON funds(form_d_file_no);

-- Extra Form D file numbers of funds that list more than one (funds.form_d_file_no holds the main one).
CREATE TABLE IF NOT EXISTS fund_file_nos (
  pfid                TEXT NOT NULL REFERENCES funds(pfid),
  file_no             TEXT NOT NULL,
  PRIMARY KEY (pfid, file_no)
);
CREATE INDEX IF NOT EXISTS idx_fund_file_nos_file_no ON fund_file_nos(file_no);

CREATE TABLE IF NOT EXISTS formd_offerings (
  accession_no        TEXT PRIMARY KEY,
  file_no             TEXT,
  cik                 TEXT NOT NULL,
  issuer_name         TEXT NOT NULL,
  is_amendment        INTEGER NOT NULL,
  is_fund             INTEGER NOT NULL,
  fund_type           TEXT,
  filing_date         TEXT NOT NULL,
  first_sale_date     TEXT,
  total_offering_usd  INTEGER,
  offering_indefinite INTEGER NOT NULL DEFAULT 0,
  total_sold_usd      INTEGER,
  investors_count     INTEGER,
  min_investment_usd  INTEGER,
  state               TEXT,
  related_persons     TEXT
);
CREATE INDEX IF NOT EXISTS idx_formd_file_no ON formd_offerings(file_no);
CREATE INDEX IF NOT EXISTS idx_formd_new_funds ON formd_offerings(filing_date) WHERE is_fund = 1 AND is_amendment = 0;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
