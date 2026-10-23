ALTER TABLE alert_states ADD strategy_data text;
--> statement-breakpoint
CREATE TABLE queue_session_peaks (
    ins_code text NOT NULL,
    session_id text NOT NULL,
    first_observed_at integer NOT NULL,
    last_observed_at integer NOT NULL,
    buy_volume integer NOT NULL DEFAULT 0,
    buy_timestamp integer,
    sell_volume integer NOT NULL DEFAULT 0,
    sell_timestamp integer,
    PRIMARY KEY (ins_code, session_id)
);
--> statement-breakpoint
CREATE INDEX queue_session_peaks_retention_idx ON queue_session_peaks(last_observed_at);
