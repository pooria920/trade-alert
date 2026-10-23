ALTER TABLE alert_rules ADD quality_config text;
--> statement-breakpoint
ALTER TABLE alert_states ADD quality_data text;
--> statement-breakpoint
ALTER TABLE alert_events ADD severity text NOT NULL DEFAULT 'normal';
--> statement-breakpoint
CREATE INDEX alert_events_created_idx ON alert_events(created_at);
--> statement-breakpoint
CREATE TABLE alert_outcomes (
 id integer PRIMARY KEY,
 event_id text REFERENCES alert_events(id) ON DELETE SET NULL,
 source_event_id text NOT NULL,
 owner_id text NOT NULL REFERENCES users(telegram_user_id),
 channel_id text REFERENCES telegram_channels(channel_id),
 ins_code text NOT NULL REFERENCES symbols(ins_code),
 session_id text NOT NULL,
 window_minutes integer NOT NULL,
 target_at integer NOT NULL,
 cutoff_at integer NOT NULL,
 observed_at integer,
 initial_price real,
 observed_price real,
 price_metric text,
 initial_queue_volume integer NOT NULL,
 observed_queue_volume integer,
 queue_side text NOT NULL,
 price_change_percent real,
 queue_change_percent real,
 status text NOT NULL DEFAULT 'pending',
 UNIQUE(source_event_id, window_minutes)
);
--> statement-breakpoint
CREATE INDEX alert_outcomes_pending_idx ON alert_outcomes(status, ins_code, target_at);
--> statement-breakpoint
CREATE INDEX alert_outcomes_due_idx ON alert_outcomes(status, target_at, id);
--> statement-breakpoint
CREATE INDEX alert_outcomes_retention_idx ON alert_outcomes(target_at);
--> statement-breakpoint
CREATE TABLE alert_suppressions (
 rule_id text NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
 day_at integer NOT NULL,
 count integer NOT NULL DEFAULT 0,
 PRIMARY KEY(rule_id, day_at)
);
--> statement-breakpoint
CREATE INDEX alert_suppressions_day_idx ON alert_suppressions(day_at);
