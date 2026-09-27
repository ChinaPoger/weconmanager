from wecom_svr import localdb


def test_real_id_columns_come_back_as_int(tmp_path):
    """REAL 身份列在连接层就收成 int,别的 REAL 列不动。"""
    db = tmp_path / "Info.db"
    conn = localdb.open_db(db)
    conn.execute("CREATE TABLE MESSAGE (LID REAL, conv_id REAL, sender_id REAL, ratio REAL)")
    conn.execute("INSERT INTO MESSAGE VALUES (?, ?, ?, ?)",
                 (10945118031183244.0, 10945118031183244.0, 42.0, 0.5))
    conn.commit()

    row = conn.execute("SELECT m.LID, conv_id AS RID, sender_id, ratio FROM MESSAGE m").fetchone()
    conn.close()

    assert row["LID"] == row["RID"] == 10945118031183244
    assert isinstance(row["sender_id"], int)
    assert f"{row['LID']}" == "10945118031183244"
    assert row["ratio"] == 0.5
