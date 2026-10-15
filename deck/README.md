# Báo cáo Task 1 — Attack deck

Deck gồm 14 lá hợp lệ: 10 attack và 4 blank. Lineup khóa 10 attack, không lặp.

Phân bố attack:

| Layer | Số lá | Lớp lỗi |
|---|---:|---|
| MCP | 4 | shadow, poisoned_result, drift, schema_bomb |
| Gateway | 2 | replica_flip, header_spoof |
| A2A | 4 | identity, forged_card, faithless_peer |

Tổng cộng deck bao phủ đủ 9 lớp duel. Hai mutation `swap_replica` dùng path nằm
trong drift set thật của world `df8c55dabb35`. Các ask và anchor đã được kiểm tra
trên corpus 12.375 trang.

Lineup bắt đầu bằng catalog pressure, sau đó luân phiên content, identity và
routing attacks. Hai identity attack được đặt ở cuối để tận dụng hệ số vòng cao.
Chi tiết lựa chọn và giới hạn của phép đo local nằm trong `STRATEGY.md`.

Kết quả validator: không có lỗi cấu trúc hoặc corpus. Cảnh báo lethality-band còn
lại vì mutation engine đầy đủ chỉ tồn tại trong Arena của giảng viên.
