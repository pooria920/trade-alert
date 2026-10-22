CREATE TABLE `alert_events` (
	`id` text PRIMARY KEY NOT NULL,
	`rule_id` text NOT NULL,
	`message` text NOT NULL,
	`created_at` integer NOT NULL,
	`sent_at` integer,
	FOREIGN KEY (`rule_id`) REFERENCES `alert_rules`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `alert_events_rule_time_idx` ON `alert_events` (`rule_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `alert_rules` (
	`id` text PRIMARY KEY NOT NULL,
	`ins_code` text NOT NULL,
	`type` text NOT NULL,
	`threshold_percent` real,
	`window_minutes` integer DEFAULT 1 NOT NULL,
	`minimum_baseline_volume` integer DEFAULT 1 NOT NULL,
	`cooldown_seconds` integer DEFAULT 0 NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`armed` integer DEFAULT true NOT NULL,
	`last_triggered_at` integer,
	`created_at` integer NOT NULL,
	FOREIGN KEY (`ins_code`) REFERENCES `symbols`(`ins_code`) ON UPDATE no action ON DELETE cascade,
	CONSTRAINT "alert_rule_type" CHECK("alert_rules"."type" IN ('BUY_QUEUE_DROP_PERCENT', 'SELL_QUEUE_DROP_PERCENT', 'BUY_QUEUE_DISAPPEARED', 'SELL_QUEUE_DISAPPEARED')),
	CONSTRAINT "alert_rule_threshold" CHECK(("alert_rules"."type" NOT IN ('BUY_QUEUE_DROP_PERCENT', 'SELL_QUEUE_DROP_PERCENT')) OR ("alert_rules"."threshold_percent" IS NOT NULL AND "alert_rules"."threshold_percent" > 0 AND "alert_rules"."threshold_percent" <= 100)),
	CONSTRAINT "alert_rule_window" CHECK("alert_rules"."window_minutes" IN (1, 5)),
	CONSTRAINT "alert_rule_baseline" CHECK("alert_rules"."minimum_baseline_volume" > 0),
	CONSTRAINT "alert_rule_cooldown" CHECK("alert_rules"."cooldown_seconds" >= 0)
);
--> statement-breakpoint
CREATE INDEX `alert_rules_enabled_symbol_idx` ON `alert_rules` (`enabled`,`ins_code`);--> statement-breakpoint
CREATE TABLE `application_state` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `queue_snapshots` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`ins_code` text NOT NULL,
	`timestamp` integer NOT NULL,
	`queue_type` text NOT NULL,
	`volume` integer NOT NULL,
	`price` real,
	`order_count` integer NOT NULL,
	FOREIGN KEY (`ins_code`) REFERENCES `symbols`(`ins_code`) ON UPDATE no action ON DELETE cascade,
	CONSTRAINT "snapshot_queue_type" CHECK("queue_snapshots"."queue_type" IN ('buy', 'sell', 'none')),
	CONSTRAINT "snapshot_quantities" CHECK("queue_snapshots"."volume" >= 0 AND "queue_snapshots"."order_count" >= 0),
	CONSTRAINT "snapshot_price" CHECK("queue_snapshots"."price" IS NULL OR "queue_snapshots"."price" > 0)
);
--> statement-breakpoint
CREATE UNIQUE INDEX `queue_snapshots_symbol_time_idx` ON `queue_snapshots` (`ins_code`,`timestamp`);--> statement-breakpoint
CREATE INDEX `queue_snapshots_retention_idx` ON `queue_snapshots` (`timestamp`);--> statement-breakpoint
CREATE TABLE `symbols` (
	`ins_code` text PRIMARY KEY NOT NULL,
	`symbol` text NOT NULL,
	`created_at` integer NOT NULL
);
