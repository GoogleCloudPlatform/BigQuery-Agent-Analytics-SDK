-- Synthetic fixture for examples/okf_attested_computation (SYNTHETIC data).
-- Provisioning path only. Runtime callers never need write access.
-- Dataset: test-project-0728-467323.okf_receipt_spike_20260905 (location US).
-- Run with: bq query --project_id=test-project-0728-467323 --location=US --nouse_legacy_sql < fixture.sql

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.orders` (
  order_id STRING NOT NULL,
  customer_id STRING,
  order_ts TIMESTAMP NOT NULL,
  order_status STRING NOT NULL,
  gross_amount NUMERIC,
  discount_amount NUMERIC,
  net_amount NUMERIC NOT NULL,
  shipping_amount NUMERIC,
  tax_amount NUMERIC,
  currency STRING NOT NULL,
  channel STRING
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.fx_daily_rates` (
  currency STRING NOT NULL,
  rate_date DATE NOT NULL,
  rate_to_usd NUMERIC NOT NULL
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.order_lines` (
  order_id STRING NOT NULL,
  product_id STRING NOT NULL,
  quantity INT64 NOT NULL
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.products` (
  product_id STRING NOT NULL,
  cost NUMERIC NOT NULL
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.fulfillment_cost` (
  order_id STRING NOT NULL,
  allocated_cost NUMERIC NOT NULL
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.shipment_cost` (
  order_id STRING NOT NULL,
  shipping_cost NUMERIC NOT NULL
);

CREATE OR REPLACE TABLE `okf_receipt_spike_20260905.payment_fees` (
  order_id STRING NOT NULL,
  fee_amount NUMERIC NOT NULL
);

-- January delivered order: revenue 1000, full COGS 600 -> margin 400.
-- January cancelled order: excluded by order_status.
-- February delivered order: revenue 200, full COGS 85 -> margin 115 (Jan+Feb = 515).
INSERT INTO `okf_receipt_spike_20260905.orders`
  (order_id, customer_id, order_ts, order_status, gross_amount, discount_amount, net_amount, shipping_amount, tax_amount, currency, channel)
VALUES
  ('ord-2026-01-delivered', 'cust-1', TIMESTAMP '2026-01-10 12:00:00+00', 'delivered', NUMERIC '1000.00', NUMERIC '0', NUMERIC '1000.00', NUMERIC '0', NUMERIC '0', 'USD', 'web'),
  ('ord-2026-01-cancelled', 'cust-2', TIMESTAMP '2026-01-11 12:00:00+00', 'cancelled', NUMERIC '999.00', NUMERIC '0', NUMERIC '999.00', NUMERIC '0', NUMERIC '0', 'USD', 'web'),
  ('ord-2026-02-delivered', 'cust-3', TIMESTAMP '2026-02-10 12:00:00+00', 'delivered', NUMERIC '200.00', NUMERIC '0', NUMERIC '200.00', NUMERIC '0', NUMERIC '0', 'USD', 'mobile');

INSERT INTO `okf_receipt_spike_20260905.order_lines` (order_id, product_id, quantity) VALUES
  ('ord-2026-01-delivered', 'sku-400', 1),
  ('ord-2026-01-cancelled', 'sku-400', 1),
  ('ord-2026-02-delivered', 'sku-50', 1);

INSERT INTO `okf_receipt_spike_20260905.products` (product_id, cost) VALUES
  ('sku-400', NUMERIC '400.00'),
  ('sku-50', NUMERIC '50.00');

INSERT INTO `okf_receipt_spike_20260905.fulfillment_cost` (order_id, allocated_cost) VALUES
  ('ord-2026-01-delivered', NUMERIC '100.00'),
  ('ord-2026-02-delivered', NUMERIC '20.00');

INSERT INTO `okf_receipt_spike_20260905.shipment_cost` (order_id, shipping_cost) VALUES
  ('ord-2026-01-delivered', NUMERIC '50.00'),
  ('ord-2026-02-delivered', NUMERIC '10.00');

INSERT INTO `okf_receipt_spike_20260905.payment_fees` (order_id, fee_amount) VALUES
  ('ord-2026-01-delivered', NUMERIC '50.00'),
  ('ord-2026-02-delivered', NUMERIC '5.00');
