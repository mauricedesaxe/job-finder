-- Reviews become revisable: each submit appends a new immutable event and the
-- latest event per item is the effective one. The one-event-per-item index
-- would reject revisions, and the freeform feedback flow no longer carries a
-- primary reason taxonomy, so the column becomes optional.
DROP INDEX one_review_event_per_item;
ALTER TABLE review_events ALTER COLUMN primary_reason DROP NOT NULL;
CREATE INDEX review_events_item_recency
ON review_events(review_item_id, created_at DESC, id DESC);
