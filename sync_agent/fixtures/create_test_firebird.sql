/*
  create_test_firebird.sql

  Creates the pdv_test.fdb database with a PRODUTO table that matches
  the placeholder query in firebird_reader.py.

  Columns: ID, CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA

  Note: accented characters are intentionally avoided in the test data
  to prevent charset issues during local setup. The real POS data will
  have accents — charset handling is tested separately after the real
  schema is confirmed.
*/

/* ============================================================
   1. Create database
   ============================================================ */

CREATE DATABASE 'C:\test_data\pdv_test.fdb'
    USER 'SYSDBA' PASSWORD 'masterkey'
    PAGE_SIZE 8192
    DEFAULT CHARACTER SET WIN1252;

/* ============================================================
   2. Products table — mirrors the expected POS schema
   ============================================================ */

CREATE TABLE PRODUTO (
    ID        INTEGER        NOT NULL,
    CODIGO    VARCHAR(30),
    DESCRICAO VARCHAR(200)   NOT NULL,
    PRECO     NUMERIC(10, 2) NOT NULL,
    ESTOQUE   NUMERIC(12, 3) NOT NULL DEFAULT 0,
    CATEGORIA VARCHAR(100),
    CONSTRAINT PK_PRODUTO PRIMARY KEY (ID)
);

COMMIT;

/* ============================================================
   3. Auto-increment — GENERATOR + TRIGGER (Firebird 2.5 pattern)
   ============================================================ */

CREATE GENERATOR GEN_PRODUTO_ID;
SET GENERATOR GEN_PRODUTO_ID TO 0;

COMMIT;

SET TERM !! ;

CREATE TRIGGER PRODUTO_BI FOR PRODUTO
    ACTIVE BEFORE INSERT POSITION 0
AS BEGIN
    IF (NEW.ID IS NULL) THEN
        NEW.ID = GEN_ID(GEN_PRODUTO_ID, 1);
END !!

SET TERM ; !!

COMMIT;

/* ============================================================
   4. Test data — 20 products across 7 categories
      Two products (Banana, Tomate, Pao Frances) have no barcode
      to test nullable CODIGO handling.
   ============================================================ */

/* Graos */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752865', 'Arroz Branco Tipo 1 5kg', 24.90, 50.000, 'Graos');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752872', 'Feijao Carioca 1kg', 8.50, 80.000, 'Graos');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752889', 'Macarrao Espaguete 500g', 4.20, 120.000, 'Graos');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752896', 'Acucar Cristal 1kg', 4.50, 90.000, 'Graos');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752902', 'Sal Refinado 1kg', 3.10, 75.000, 'Graos');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752919', 'Oleo de Soja 900ml', 7.90, 55.000, 'Graos');

/* Laticinios */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752926', 'Leite Integral UHT 1L', 6.90, 60.000, 'Laticinios');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752933', 'Manteiga sem Sal 200g', 11.90, 25.000, 'Laticinios');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752940', 'Iogurte Natural 500g', 7.50, 30.000, 'Laticinios');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752957', 'Queijo Mussarela 300g', 14.90, 25.000, 'Laticinios');

/* Bebidas */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752964', 'Agua Mineral 1.5L', 3.50, 100.000, 'Bebidas');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752971', 'Refrigerante Cola 2L', 9.90, 45.000, 'Bebidas');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752988', 'Suco de Laranja 1L', 8.90, 35.000, 'Bebidas');

/* Higiene */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006752995', 'Sabonete Neutro 90g', 3.20, 70.000, 'Higiene');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006753009', 'Shampoo 400ml', 15.90, 20.000, 'Higiene');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006753016', 'Papel Higienico 12un', 19.90, 40.000, 'Higiene');

/* Hortifruti — sem codigo de barras (NULL) */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES (NULL, 'Banana Prata 1kg', 5.90, 30.000, 'Hortifruti');

INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES (NULL, 'Tomate 1kg', 8.90, 20.000, 'Hortifruti');

/* Carnes */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES ('7896006753023', 'Frango Congelado 1kg', 12.90, 15.000, 'Carnes');

/* Padaria — sem codigo de barras (NULL) */
INSERT INTO PRODUTO (CODIGO, DESCRICAO, PRECO, ESTOQUE, CATEGORIA)
VALUES (NULL, 'Pao Frances', 0.75, 200.000, 'Padaria');

COMMIT;

EXIT;
