CREATE TABLE `telegram_channels` (
	`channel_id` text PRIMARY KEY NOT NULL,
	`title` text DEFAULT 'کانال تنظیم‌شده' NOT NULL,
	`can_post` integer DEFAULT false NOT NULL,
	`checked_at` integer,
	`permission_error` text
);
--> statement-breakpoint
ALTER TABLE `alert_rules` ADD `channel_id` text REFERENCES telegram_channels(channel_id);--> statement-breakpoint
CREATE INDEX `alert_rules_channel_idx` ON `alert_rules` (`channel_id`,`enabled`);--> statement-breakpoint
ALTER TABLE `alert_states` ADD `recovery_data` text;