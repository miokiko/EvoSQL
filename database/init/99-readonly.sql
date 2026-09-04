-- The MySQL image initially grants the application user full access so that
-- init files can run. Replace that grant after the copied dump is imported.
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'evo_text2sql_ro'@'%';
GRANT SELECT, SHOW VIEW ON `evo_text2sql_eval`.* TO 'evo_text2sql_ro'@'%';
FLUSH PRIVILEGES;
