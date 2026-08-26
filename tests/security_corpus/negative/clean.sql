SELECT id, name FROM users WHERE email = {{ email }};
INSERT INTO logs (event) VALUES ({{ event }});