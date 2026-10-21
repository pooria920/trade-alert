CREATE TABLE `alert_states` (
	`rule_id` text PRIMARY KEY NOT NULL,
	`revision` integer NOT NULL,
	`session_id` text NOT NULL,
	`status` text NOT NULL,
	`last_evaluated_at` integer NOT NULL,
	`last_triggered_at` integer,
	`baseline_volume` integer,
	FOREIGN KEY (`rule_id`) REFERENCES `alert_rules`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE TABLE `notification_outbox` (
	`event_id` text PRIMARY KEY NOT NULL,
	`status` text DEFAULT 'PENDING' NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL,
	`next_attempt_at` integer NOT NULL,
	`lease_until` integer,
	`lease_token` text,
	`error_code` text,
	FOREIGN KEY (`event_id`) REFERENCES `alert_events`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `notification_outbox_due_idx` ON `notification_outbox` (`status`,`next_attempt_at`);--> statement-breakpoint
ALTER TABLE `alert_events` ADD `occurrence_key` text;--> statement-breakpoint
ALTER TABLE `alert_events` ADD `payload` text;--> statement-breakpoint
ALTER TABLE `alert_events` ADD `rule_revision` integer;--> statement-breakpoint
CREATE UNIQUE INDEX `alert_events_occurrence_idx` ON `alert_events` (`occurrence_key`);--> statement-breakpoint
ALTER TABLE `alert_rules` ADD `definition` text;--> statement-breakpoint
ALTER TABLE `alert_rules` ADD `revision` integer DEFAULT 1 NOT NULL;--> statement-breakpoint
ALTER TABLE `queue_snapshots` ADD `session_id` text;
--> statement-breakpoint
UPDATE alert_rules SET definition = CASE
  WHEN type IN ('BUY_QUEUE_DROP_PERCENT','SELL_QUEUE_DROP_PERCENT')
    THEN json_object('type',type,'config',json_object('thresholdPercent',threshold_percent,'windowMinutes',window_minutes,'minimumBaselineVolume',minimum_baseline_volume))
  ELSE json_object('type',type,'config',json_object('minimumPreviousVolume',minimum_baseline_volume))
END WHERE owner_id IS NOT NULL;
