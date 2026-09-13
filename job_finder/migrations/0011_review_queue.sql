ALTER TABLE review_items
DROP CONSTRAINT review_items_review_day_lane_position_key;

CREATE INDEX review_items_day_lane_position_idx
ON review_items (review_day, lane, position);
