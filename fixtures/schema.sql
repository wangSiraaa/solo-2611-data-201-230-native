CREATE TABLE customers(customer_id text PRIMARY KEY,name text NOT NULL CHECK(length(name)>0));
CREATE TABLE orders(order_id text PRIMARY KEY,customer_id text NOT NULL REFERENCES customers(customer_id),amount numeric(12,2) NOT NULL CHECK(amount>=0),note text NOT NULL);
CREATE TABLE import_receipts(run_id text NOT NULL,table_name text NOT NULL,logical_row bigint NOT NULL,accepted boolean NOT NULL,source_key text,sqlstate text,constraint_name text,PRIMARY KEY(run_id,table_name,logical_row));
