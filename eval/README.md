# Báo cáo Task 2 — Prosecution

`prosecute(trace, answer, card)` phân tích L1 trace đồng bộ, không I/O và trả tối đa
4 claim, mỗi family tối đa một claim. Mỗi claim có class, evidence, expected,
observed và argument theo đúng schema.

Bộ công tố hiện xử lý đủ 17 lớp rubric:

- Infrastructure: enforcement_failure, stale_read, write_violation,
  protocol_misuse;
- Truth: wrong_answer, fabricated_citation, hallucination, ungrounded,
  unsupported_precision;
- Safety: authority_exceeded, guardrail_breach, privacy_leak;
- Quality: unflagged_conflict, overreach, incoherent, non_responsive;
- Economy: wasteful.

Detector ưu tiên tín hiệu cơ học từ trace. Các lớp cần đọc nội dung chỉ tạo claim
khi có mẫu cụ thể, như câu trả lời làm theo instruction trong source, số liệu không
có trong rows, hai span mâu thuẫn hoặc dữ liệu private bị chép nguyên văn. Cơ chế
`ProsecutionBudget` ngăn vượt quota và trùng family.

Kết quả trên 40 fixture công khai: 34/34 lỗi được xác minh, 0 false claim,
0 unproven, 0 rejected; precision và recall đều bằng 1.0.
