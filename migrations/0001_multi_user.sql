CREATE TABLE `users` (
	`telegram_user_id` text PRIMARY KEY NOT NULL,
	`private_chat_id` text NOT NULL,
	`notifications_enabled` integer DEFAULT true NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `watchlists` (
	`user_id` text NOT NULL,
	`ins_code` text NOT NULL,
	`created_at` integer NOT NULL,
	PRIMARY KEY(`user_id`, `ins_code`),
	FOREIGN KEY (`user_id`) REFERENCES `users`(`telegram_user_id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`ins_code`) REFERENCES `symbols`(`ins_code`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `watchlists_symbol_idx` ON `watchlists` (`ins_code`);--> statement-breakpoint
ALTER TABLE `alert_rules` ADD `owner_id` text REFERENCES users(telegram_user_id);--> statement-breakpoint
CREATE INDEX `alert_rules_owner_symbol_idx` ON `alert_rules` (`owner_id`,`ins_code`);