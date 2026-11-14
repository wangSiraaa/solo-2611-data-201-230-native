CREATE TABLE outer_rows(row_id INTEGER PRIMARY KEY, customer_id INTEGER, probe INTEGER);
CREATE TABLE payments(payment_id INTEGER PRIMARY KEY, customer_id INTEGER, code INTEGER, amount INTEGER);
CREATE TABLE flags(flag_id INTEGER PRIMARY KEY, customer_id INTEGER, value TEXT);
INSERT INTO outer_rows VALUES(1,10,7),(2,10,8),(3,20,8),(4,30,NULL),(5,40,NULL),(6,NULL,7),(7,50,9);
INSERT INTO payments VALUES(1,10,7,20),(2,10,NULL,30),(3,20,9,40),(4,30,NULL,50),(5,99,1,60);
INSERT INTO flags VALUES(1,10,'ok'),(2,20,NULL),(3,50,'a'),(4,50,'b');
