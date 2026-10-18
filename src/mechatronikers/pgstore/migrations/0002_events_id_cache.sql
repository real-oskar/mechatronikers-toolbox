-- Paging events by id (`events(after=<id>)`, SSE) relies on ingest taking ids in the order it takes
-- its advisory locks (PgEventRepository, #73 phase B). A sequence cache above 1 lets each backend
-- hand out ids from its own pre-allocated block, so a later writer can get a lower id than an
-- earlier one and readers skip rows. CACHE 1 is the default; this makes it explicit, and
-- PgStore.open() refuses a database where it was raised (e.g. as a throughput tweak).
ALTER TABLE events ALTER COLUMN id SET CACHE 1;
