CREATE SCHEMA split_fixture;
CREATE TABLE split_fixture.records(
 id bigint PRIMARY KEY, tenant text NOT NULL, name text NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 note text, attachment bytea);
INSERT INTO split_fixture.records VALUES
 (1,'east','甲','active',NULL,NULL),
 (2,'east','乙','disabled','',decode('','hex')),
 (3,'west','丙','active','详细备注',decode('0001ff','hex')),
 (4,'west','丁','active',NULL,decode('00','hex'));
