-- Whether each snapshot line was buyable on the website, and its product page.

ALTER TABLE stock_snapshot ADD COLUMN available_online BOOLEAN;
ALTER TABLE stock_snapshot ADD COLUMN url TEXT;
