CREATE TABLE IF NOT EXISTS "images" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"potd_id"	INTEGER,
	"image"	BLOB,
	FOREIGN KEY("potd_id") REFERENCES "problems"("id")
);
CREATE TABLE IF NOT EXISTS "attempts" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"user_id"	INTEGER NOT NULL,
	"potd_id"	INTEGER NOT NULL,
	"official"	BOOLEAN,
	"submission"	INTEGER,
	"submit_time"	DATETIME,
	FOREIGN KEY("user_id") REFERENCES "users"("discord_id"),
	FOREIGN KEY("potd_id") REFERENCES "problems"("id")
);
CREATE TABLE IF NOT EXISTS "ratings" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"userid"	INTEGER,
	"problemid"	INTEGER,
	"rating"	INTEGER,
	FOREIGN KEY("userid") REFERENCES "users"("discord_id"),
	FOREIGN KEY("problemid") REFERENCES "problems"("id")
);
CREATE TABLE IF NOT EXISTS "seasons" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"running"	BOOLEAN NOT NULL,
	"latest_potd"	INTEGER,
	"name"	TEXT,
	"bronze_cutoff" INTEGER,
	"silver_cutoff" INTEGER,
	"gold_cutoff"   INTEGER,
	"public"        BOOLEAN,
	FOREIGN KEY("latest_potd") REFERENCES "problems"("id")
);
CREATE TABLE IF NOT EXISTS "solves" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"user"	INTEGER,
	"problem_id"	INTEGER,
	"num_attempts"	INTEGER,
	"official"	BOOLEAN,
	FOREIGN KEY("user") REFERENCES "users"("discord_id"),
	FOREIGN KEY("problem_id") REFERENCES "problems"("id")
);
CREATE TABLE IF NOT EXISTS "rankings" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"season_id"	INTEGER,
	"user_id"	INTEGER,
	"rank"	INTEGER,
	"score"	REAL,
	UNIQUE ("season_id", "user_id"),
	FOREIGN KEY("user_id") REFERENCES "users"("discord_id"),
	FOREIGN KEY("season_id") REFERENCES "seasons"("id")
);
CREATE TABLE IF NOT EXISTS "users" (
	"discord_id"	INTEGER NOT NULL UNIQUE,
	"nickname"	TEXT,
	"anonymous"	BOOLEAN,
	"receiving_medal_roles"   BOOLEAN DEFAULT 1,
	PRIMARY KEY("discord_id")
);
CREATE TABLE IF NOT EXISTS "problems" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"date"	DATE NOT NULL,
	"season"	INTEGER NOT NULL,
	"statement"	TEXT NOT NULL,
	"difficulty"	INTEGER,
	"weighted_solves"	INTEGER NOT NULL DEFAULT 0,
	"base_points"	INTEGER NOT NULL DEFAULT 0,
	"answer"	INTEGER NOT NULL,
	"manual_marking"	BOOLEAN NOT NULL DEFAULT 0,
	"public"	BOOLEAN,
	"source"	TEXT,
	"stats_message_id"	INTEGER,
	"difficulty_rating" REAL DEFAULT 1500,
	"coolness_rating"   REAL DEFAULT 1500,
	FOREIGN KEY("season") REFERENCES "seasons"("id")
);

CREATE TABLE IF NOT EXISTS "subproblems" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"potd_id"	INTEGER NOT NULL,
	"label"	TEXT NOT NULL,
	"statement"	TEXT NOT NULL,
	"marks"	INTEGER NOT NULL DEFAULT 1,
	"answer"	INTEGER,
	"manual_marking"	BOOLEAN NOT NULL DEFAULT 1,
	"order_index"	INTEGER NOT NULL DEFAULT 0,
	"public"	BOOLEAN NOT NULL DEFAULT 1,
	UNIQUE("potd_id", "label"),
	FOREIGN KEY("potd_id") REFERENCES "problems"("id")
);

CREATE TABLE IF NOT EXISTS "subproblem_images" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"subproblem_id"	INTEGER NOT NULL,
	"image"	BLOB,
	FOREIGN KEY("subproblem_id") REFERENCES "subproblems"("id")
);

CREATE TABLE IF NOT EXISTS "subproblem_threads" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"subproblem_id"	INTEGER NOT NULL,
	"server_id"	INTEGER NOT NULL,
	"channel_id"	INTEGER NOT NULL,
	"message_id"	INTEGER NOT NULL,
	"thread_id"	INTEGER,
	FOREIGN KEY("subproblem_id") REFERENCES "subproblems"("id")
);

CREATE TABLE IF NOT EXISTS "subproblem_attempts" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"user_id"	INTEGER NOT NULL,
	"potd_id"	INTEGER NOT NULL,
	"subproblem_id"	INTEGER NOT NULL,
	"submission"	INTEGER,
	"submit_time"	DATETIME NOT NULL,
	"is_correct"	BOOLEAN NOT NULL DEFAULT 0,
	FOREIGN KEY("user_id") REFERENCES "users"("discord_id"),
	FOREIGN KEY("potd_id") REFERENCES "problems"("id"),
	FOREIGN KEY("subproblem_id") REFERENCES "subproblems"("id")
);

CREATE TABLE IF NOT EXISTS "subproblem_solves" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"user_id"	INTEGER NOT NULL,
	"potd_id"	INTEGER NOT NULL,
	"subproblem_id"	INTEGER NOT NULL,
	"num_attempts"	INTEGER NOT NULL,
	"official"	BOOLEAN NOT NULL DEFAULT 1,
	UNIQUE("user_id", "subproblem_id", "official"),
	FOREIGN KEY("user_id") REFERENCES "users"("discord_id"),
	FOREIGN KEY("potd_id") REFERENCES "problems"("id"),
	FOREIGN KEY("subproblem_id") REFERENCES "subproblems"("id")
);

CREATE TABLE IF NOT EXISTS "config" (
	"server_id"	INTEGER,
	"potd_channel"	INTEGER,
	"subproblem_thread_channel_id"	INTEGER,
	"submission_channel_id"	INTEGER,
	"submission_ping_role_id"	INTEGER,
	"auto_publish_news"	BOOLEAN NOT NULL DEFAULT 1,
	"ping_role_id"	INTEGER,
	"solved_role_id"	INTEGER,
	"otd_prefix"	TEXT,
	"command_prefix"	TEXT,
	"bronze_role_id"    INTEGER,
	"silver_role_id"    INTEGER,
	"gold_role_id"  INTEGER,
	PRIMARY KEY("server_id")
);
CREATE TABLE IF NOT EXISTS "stats_messages" (
	"id"	INTEGER,
	"potd_id"	INTEGER,
	"server_id"     INTEGER,
	"channel_id"    INTEGER,
	"message_id"	INTEGER,
	FOREIGN KEY("potd_id") REFERENCES "problems"("id"),
	PRIMARY KEY("id")
);
CREATE TABLE IF NOT EXISTS "rating_choices" (
	"id"	INTEGER NOT NULL,
	"problem_1_id"	INTEGER NOT NULL,
	"problem_2_id"	INTEGER NOT NULL,
	"choice"	INTEGER,
	"type"	TEXT,
	"rater" INTEGER,
	PRIMARY KEY("id"),
	FOREIGN KEY("problem_2_id") REFERENCES "problems"("id"),
	FOREIGN KEY("problem_1_id") REFERENCES "problems"("id")
);

CREATE TABLE IF NOT EXISTS "manual_submissions" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"user_id"	INTEGER NOT NULL,
	"potd_id"	INTEGER NOT NULL,
	"subproblem_id"	INTEGER,
	"season_id"	INTEGER NOT NULL,
	"content"	TEXT,
	"submitted_at"	DATETIME NOT NULL,
	"status"	TEXT NOT NULL DEFAULT 'pending',
	"claimed_by"	INTEGER,
	"claimed_at"	DATETIME,
	"reviewer_id"	INTEGER,
	"reviewed_at"	DATETIME,
	"decision"	BOOLEAN,
	"dm_channel_id"	INTEGER,
	"dm_message_id"	INTEGER,
	FOREIGN KEY("user_id") REFERENCES "users"("discord_id"),
	FOREIGN KEY("potd_id") REFERENCES "problems"("id"),
	FOREIGN KEY("subproblem_id") REFERENCES "subproblems"("id"),
	FOREIGN KEY("season_id") REFERENCES "seasons"("id")
);

CREATE TABLE IF NOT EXISTS "manual_submission_messages" (
	"id"	INTEGER NOT NULL PRIMARY KEY,
	"submission_id"	INTEGER NOT NULL,
	"server_id"	INTEGER NOT NULL,
	"channel_id"	INTEGER NOT NULL,
	"message_id"	INTEGER NOT NULL,
	"thread_id"	INTEGER,
	"control_message_id"	INTEGER,
	UNIQUE("server_id", "message_id"),
	FOREIGN KEY("submission_id") REFERENCES "manual_submissions"("id")
);
