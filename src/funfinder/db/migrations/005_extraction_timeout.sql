-- Статус timeout: ни одна модель не ответила за отведённое время. Такой пост
-- не валит весь прогон extract_events, а перепрогоняется в следующем запуске.

ALTER TABLE staging.vk_post_extractions
    DROP CONSTRAINT vk_post_extractions_status_check,
    ADD CONSTRAINT vk_post_extractions_status_check
        CHECK (status IN ('ok', 'skipped_empty', 'refused', 'invalid_json', 'timeout'));
