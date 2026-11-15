-- 修改一个边界:regional.middle 的上界由 (20,0) 改为 (25,0)。
-- DETACH/ATTACH 复用同一张表,OID 不变,但 relpartbound 改变,
-- 目录指纹随之改变,旧计划必须失效。不创建新分区、不迁移任何数据。
ALTER TABLE route_fixture.regional DETACH PARTITION route_fixture.middle;
ALTER TABLE route_fixture.regional ATTACH PARTITION route_fixture.middle
    FOR VALUES FROM (10,0) TO (25,0);
