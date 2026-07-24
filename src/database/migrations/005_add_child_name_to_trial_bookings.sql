-- ============================================================
-- Migration 005: Add child_name to trial_bookings
--
-- Owner of writes: bot/bookings.py::create_booking_with_lock(), called from
--                  bot/scheduling.py::book_slot() (Module 2 + Module 3 handler).
-- Owner of reads:  bot/bookings.py::list_active_bookings_by_sender() and the
--                  Calendar description patch in bot/scheduling.py, which prints
--                  the child's name for [BABY]/[CRIANCAS] bookings.
--
-- Baby and kids classes are attended by a minor, so a booking there records two
-- people: the responsible lead who chats on WhatsApp (lead_name/sender, already
-- present) and the child who actually takes the class (child_name, added here).
-- Adult classes keep a single name and leave this column NULL.
--
-- child_name is NULLABLE on purpose: NULL means "not applicable" (an adult
-- booking), which is different from an empty string standing for "a child class
-- whose name wasn't collected". Keeping them distinct lets a later query tell a
-- genuinely-incomplete child booking apart from an ordinary adult one. Nothing
-- that exists breaks: the column is nullable and every current INSERT omits it.
-- ============================================================

ALTER TABLE trial_bookings
ADD COLUMN IF NOT EXISTS child_name VARCHAR(255) NULL;
