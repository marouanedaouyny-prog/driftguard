CREATE TABLE users (id INTEGER, email TEXT);

CREATE USER reader IDENTIFIED BY 'readonly123';
GRANT SELECT ON users TO reader;