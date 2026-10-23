CREATE TABLE queue_lifecycle_states (
 ins_code text NOT NULL,
 side text NOT NULL,
 session_id text NOT NULL,
 status text NOT NULL,
 first_observed_at integer NOT NULL,
 last_source_at integer NOT NULL,
 last_volume integer,
 last_volume_at integer,
 last_queue_at integer,
 last_formed_at integer,
 last_disappeared_at integer,
 cycles integer NOT NULL DEFAULT 0,
 observation_valid integer NOT NULL DEFAULT 0,
 observation_reason text,
 continuous integer NOT NULL DEFAULT 0,
 pending_kind text,
 pending_count integer NOT NULL DEFAULT 0,
 pending_since integer,
 weak_baseline integer,
 weak_baseline_at integer,
 weak_boundary real,
 weak_levels text NOT NULL DEFAULT '[]',
 weak_last_percent real NOT NULL DEFAULT 0,
 weak_occurrence_at integer,
 cycle_peak_volume integer NOT NULL DEFAULT 0,
 cycle_peak_timestamp integer,
 policy_key text NOT NULL,
 PRIMARY KEY(ins_code, side)
);
--> statement-breakpoint
CREATE TABLE queue_lifecycle_events (
 id text PRIMARY KEY NOT NULL,
 ins_code text NOT NULL,
 side text NOT NULL,
 session_id text NOT NULL,
 event_type text NOT NULL,
 timestamp integer NOT NULL,
 previous_volume integer,
 previous_timestamp integer,
 current_volume integer NOT NULL,
 peak_volume integer,
 drop_percent real,
 disappeared_at integer,
 cycles integer NOT NULL,
 occurrence_at integer NOT NULL,
 first_observed_at integer NOT NULL,
 crossed_levels text NOT NULL DEFAULT '[]',
 UNIQUE(ins_code, side, session_id, event_type, timestamp)
);
--> statement-breakpoint
CREATE INDEX queue_lifecycle_history_idx ON queue_lifecycle_events(ins_code, session_id, timestamp);
--> statement-breakpoint
CREATE INDEX queue_lifecycle_retention_idx ON queue_lifecycle_events(timestamp);
