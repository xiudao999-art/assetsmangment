BEGIN;

ALTER TABLE material_submission
    ADD COLUMN IF NOT EXISTS requires_decode SMALLINT;

UPDATE material_submission
SET requires_decode = CASE
    WHEN drama_name = '冰柜里的呼声-混剪' THEN 1
    ELSE 0
END;

ALTER TABLE material_submission
    ALTER COLUMN requires_decode SET DEFAULT 0;

ALTER TABLE material_submission
    ALTER COLUMN requires_decode SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_material_submission_requires_decode'
    ) THEN
        ALTER TABLE material_submission
            ADD CONSTRAINT ck_material_submission_requires_decode
            CHECK (requires_decode IN (0, 1));
    END IF;
END $$;

COMMENT ON COLUMN material_submission.requires_decode
    IS '是否需要生成兼容版:0=不需要,1=需要';

COMMIT;

SELECT requires_decode, COUNT(*) AS row_count
FROM material_submission
GROUP BY requires_decode
ORDER BY requires_decode;

SELECT drama_name, requires_decode, COUNT(*) AS row_count
FROM material_submission
WHERE drama_name = '冰柜里的呼声-混剪'
GROUP BY drama_name, requires_decode;
