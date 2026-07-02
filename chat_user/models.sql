CREATE DATABASE IF NOT EXISTS chat_user_demo
DEFAULT CHARACTER SET utf8mb4
DEFAULT COLLATE utf8mb4_unicode_ci;

USE chat_user_demo;

CREATE TABLE IF NOT EXISTS users (
    user_id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_dialogues (
    dialogue_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id INT UNSIGNED NOT NULL,
    title VARCHAR(200) NOT NULL,
    turn_count INT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_dialogues_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    KEY idx_user_dialogues_user_id (user_id),
    KEY idx_user_dialogues_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dialogue_turns (
    turn_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    dialogue_id BIGINT UNSIGNED NOT NULL,
    request_content TEXT NOT NULL,
    response_title VARCHAR(200) NULL,
    response_content LONGTEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_dialogue_turns_dialogue
        FOREIGN KEY (dialogue_id) REFERENCES user_dialogues(dialogue_id)
        ON DELETE CASCADE,
    KEY idx_dialogue_turns_dialogue_id (dialogue_id),
    KEY idx_dialogue_turns_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_dialogue_turns_response (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    turn_id BIGINT UNSIGNED NOT NULL,
    resp_no INT UNSIGNED NOT NULL,
    response_title VARCHAR(200) NOT NULL,
    response_content LONGTEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_dialogue_turns_response_turn
        FOREIGN KEY (turn_id) REFERENCES dialogue_turns(turn_id)
        ON DELETE CASCADE,
    CONSTRAINT uk_user_dialogue_turns_response UNIQUE (turn_id, resp_no),
    KEY idx_user_dialogue_turns_response_turn_id (turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_dialogue_turns_img_svg (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    turn_id BIGINT UNSIGNED NOT NULL,
    svg LONGTEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_dialogue_turns_img_svg_turn
        FOREIGN KEY (turn_id) REFERENCES dialogue_turns(turn_id)
        ON DELETE CASCADE,
    KEY idx_user_dialogue_turns_img_svg_turns_id (turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_dialogue_turns_response_img_svg (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    turn_id BIGINT UNSIGNED NOT NULL,
    response_id BIGINT UNSIGNED NOT NULL,
    svg LONGTEXT NOT NULL,
    create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_dialogue_turns_response_img_svg_turn
        FOREIGN KEY (turn_id) REFERENCES dialogue_turns(turn_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_user_dialogue_turns_response_img_svg_response
        FOREIGN KEY (response_id) REFERENCES user_dialogue_turns_response(id)
        ON DELETE CASCADE,
    KEY idx_user_dialogue_turns_response_img_svg_turn_id (turn_id),
    KEY idx_user_dialogue_turns_response_img_svg_response_id (response_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS zhiling_validate (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    turn_id BIGINT UNSIGNED NOT NULL,
    response_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT '待验证',
    judge_conclusion TINYINT NULL COMMENT '1=推断正确, 0=推断错误, -1=无法判断',
    judge_content LONGTEXT NULL,
    attachment_urls LONGTEXT NULL,
    create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_zhiling_validate_turn
        FOREIGN KEY (turn_id) REFERENCES dialogue_turns(turn_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_zhiling_validate_response
        FOREIGN KEY (response_id) REFERENCES user_dialogue_turns_response(id)
        ON DELETE CASCADE,
    UNIQUE KEY uk_zhiling_validate_turn_response (turn_id, response_id),
    KEY idx_zhiling_validate_turn_id (turn_id),
    KEY idx_zhiling_validate_response_id (response_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
