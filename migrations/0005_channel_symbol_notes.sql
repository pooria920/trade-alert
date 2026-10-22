CREATE TABLE `channel_symbol_notes` (
    `channel_id` text NOT NULL REFERENCES `telegram_channels`(`channel_id`) ON DELETE cascade,
    `ins_code` text NOT NULL REFERENCES `symbols`(`ins_code`) ON DELETE cascade,
    `note` text NOT NULL,
    `updated_by` text NOT NULL REFERENCES `users`(`telegram_user_id`),
    `updated_at` integer NOT NULL,
    PRIMARY KEY (`channel_id`, `ins_code`)
);
