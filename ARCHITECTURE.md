# Biên bản kiến trúc L3A

Tài liệu này mô tả các quyết định thiết kế có thể quan sát và kiểm chứng. Tài liệu
chủ ý không chứa khóa API, chỉ dẫn, suy luận riêng tư hoặc chuỗi suy luận nội bộ.

## 1. Tổng quan hệ thống

```text
inputs/<case_id>.json
        |
        v
Điều phối viên / Bộ định tuyến
        |
        +-- Tác tử đơn hàng ------ get_order, get_order_items
        +-- Tác tử thanh toán ---- get_payment_timeline
        +-- Tác tử vận chuyển ---- get_shipment_summary
        +-- Tác tử hoàn tiền ----- get_refund_timeline
        +-- Tác tử người bán ----- get_sellers
        +-- Tác tử chính sách ---- get_policy
        |
        v
Trình xác minh --> đầu ra hợp lệ theo lược đồ + nhật ký có thể quan sát
```

Thông điệp của khách hàng và chủ đề khiếu nại chỉ là gợi ý để định tuyến, không
phải dữ liệu chuẩn. Vấn đề chính được suy ra từ trạng thái đơn hàng có thẩm quyền
và bằng chứng MCP đã được giới hạn theo thời gian. Chính sách quyết định quyền
lợi, trách nhiệm, số tiền hoàn và hành động cần thực hiện.

## 2. Phân định trách nhiệm tác tử

| Tác tử | Đầu vào | Trách nhiệm | Công cụ được phép / bàn giao |
| --- | --- | --- | --- |
| Điều phối viên | Một trường hợp | Liên kết theo `case_id`, giao nhiệm vụ có giới hạn và tổng hợp kết quả ứng viên | Không gọi công cụ bằng chứng; bàn giao kết quả ứng viên cho Trình xác minh |
| Tác tử đơn hàng | ID đơn hàng được khai báo | Xác định đơn hàng có thẩm quyền, mặt hàng hiện tại và các thực thể người bán | `get_order`, `get_order_items` |
| Tác tử thanh toán | ID đơn hàng | Xác định các khoản đã thu, giao dịch thu trùng và sai lệch đối soát | `get_payment_timeline` |
| Tác tử vận chuyển | Vòng đời đơn hàng và mặt hàng hiện tại | Phân biệt chậm do người bán hay đơn vị vận chuyển | `get_shipment_summary` |
| Tác tử hoàn tiền | ID đơn hàng | Xác định vòng đời hoàn tiền đang chờ hoặc thất bại | `get_refund_timeline` |
| Tác tử người bán | ID đơn hàng | Xác định hồ sơ người bán cho các trường hợp người bán chịu trách nhiệm | `get_sellers` |
| Tác tử chính sách | Phiên bản chính sách | Chọn quy tắc dạng máy đọc được phù hợp | `get_policy` |
| Trình xác minh | Kết quả ứng viên và các tham chiếu bằng chứng đã được sử dụng | Thực thi các bất biến trước khi hoàn tất | Không gọi công cụ MCP |

Các công cụ không được chia sẻ tùy tiện. Tác tử chuyên trách hoàn tiền và người bán
chỉ được gọi khi giả thuyết của trường hợp cần đến miền dữ liệu tương ứng. Bằng chứng
vận chuyển được kiểm tra đối với các đơn đã giao để nhãn do khách hàng cung cấp
không thể lấn át dòng thời gian thực tế.

## 3. Giao thức A2A

Phong bì giao tiếp có thể quan sát được biểu diễn bằng các sự kiện nhật ký thay vì
suy luận riêng tư. Mỗi sự kiện mang theo `case_id`, tác tử, dấu thời gian và mã
quyết định khi phù hợp.

```text
case_received
  -> task_assigned(coordinator -> specialist)
  -> tool_result_consumed(specialist, evidence_ref)
  -> handoff(specialist -> coordinator)
  -> policy_decided
  -> handoff(coordinator -> verifier)
  -> verification_completed
  -> case_finalized
```

Mỗi tác tử chuyên trách nhận một danh sách hữu hạn các lần gọi công cụ và chỉ trả kết
quả một lần. Không có cơ chế giao việc đệ quy, nhờ đó tránh vòng lặp A2A. Mỗi lần
gọi công cụ được thử tối đa ba lần. `case_id` là khóa liên kết và cô lập bằng chứng.

## 4. Vòng đời bằng chứng

1. Gateway xác thực mọi phản hồi MCP theo
   `mcp-evidence-response-v1.schema.json`.
2. Tác tử chuyên trách kiểm tra miền bằng chứng trả về có khớp với công cụ đã yêu cầu
   hay không, sau đó ghi `tool_result_consumed` cùng tham chiếu do máy chủ cấp.
3. Bằng chứng thô chỉ được giữ trong bộ nhớ cho một trường hợp. Bằng chứng không bao giờ
   được lưu đệm giữa các trường hợp; tham chiếu bằng chứng cũng không bao giờ được sinh hoặc
   biến đổi cục bộ.
4. Các dòng mặt hàng mâu thuẫn được xử lý theo vòng đời đơn hàng hiện tại: với mỗi
   mặt hàng, chọn thời hạn giao cho đơn vị vận chuyển đầu tiên tại hoặc sau thời điểm
   mua hàng.
5. Các sự kiện thanh toán, hoàn tiền và vận chuyển được giới hạn trong khoảng từ
   thời điểm mua đến `opened_at`; sự kiện nhiễu trong quá khứ hoặc tương lai không
   thể làm thay đổi quyết định.
6. Tham chiếu ở cấp khiếu nại chỉ chứa bằng chứng hỗ trợ cho kết luận của khiếu nại đó.
   Tham chiếu cấp cao nhất là tập hợp đã loại trùng cần thiết cho đầu ra và quyết định
   chính sách.

## 5. Chính sách xử lý lỗi

| Lỗi | Thử lại | Phương án dự phòng | Kết quả có thể quan sát |
| --- | --- | --- | --- |
| MCP hết thời gian chờ hoặc lỗi công cụ tạm thời | Tối đa 3 lần với thời gian chờ tăng dần có giới hạn | Dừng lượt chạy thay vì tự tạo bằng chứng | CLI báo lỗi; không tạo gói nộp bài |
| Không tìm thấy bằng chứng bắt buộc | Áp dụng cùng cơ chế thử lại có giới hạn | Dừng trường hợp/lượt chạy | Không tạo thực thể hoặc tham chiếu bằng chứng giả |
| Các nguồn mâu thuẫn | Không thử lại khi cả hai nguồn đều hợp lệ | Chọn chính sách dạng máy đọc được để xác định quyền lợi; ghi `data_conflicts` | `POLICY_AUTHORITY_SELECTED` |
| Phong bì/miền bằng chứng không hợp lệ | Tối đa 3 lần | Dừng lượt chạy | Lỗi xác thực |
| Đầu ra ứng viên không hợp lệ | Không thử lại | Dừng trước `case_finalized` | Trình xác minh báo lỗi |

Các lần thử lại là thao tác đọc có tính lũy đẳng và không bao giờ thay đổi tham số công cụ
hoặc `case_id`.

## 6. Các bất biến xác minh

Trước `verification_completed`, trình xác minh kiểm tra:

- `case_id` của đầu ra bằng `case_id` của trường hợp đầu vào;
- mọi tham chiếu bằng chứng được nộp đều đã được sử dụng trong chính trường hợp này;
- các ID khiếu nại khớp chính xác với danh sách khiếu nại đầu vào;
- bằng chứng của khiếu nại là tập con của bằng chứng cấp cao nhất;
- tổng các dòng hoàn tiền bằng chính xác `recommended_refund_brl`;
- `no_action` không bao giờ đề xuất số tiền hoàn lớn hơn 0;
- ID thực thể đến từ dữ liệu MCP hoặc quy tắc chính sách đã chọn;
- giá trị tiền tệ được chuẩn hóa đến hai chữ số thập phân;
- độ tin cậy nằm trong giới hạn của lược đồ;
- JSON Schema công khai được CLI xác thực trước khi ghi từng đầu ra.

## 7. Khả năng tái lập

- Môi trường chạy: Python 3.11 trở lên; phát triển bằng Python 3.12.
- Khoảng phiên bản thư viện phụ thuộc được khai báo trong `pyproject.toml`.
- Quy trình xử lý: máy trạng thái bất đồng bộ và tất định; không sử dụng LLM hoặc hạt giống
  ngẫu nhiên.
- Mức đồng thời: xử lý từng trường hợp một để giữ thứ tự nhật ký và đơn giản hóa việc cô
  lập bằng chứng.
- Thử lại công cụ: ba lần, với thời gian chờ 0,25 và 0,50 giây.
- Các lệnh: `day09 validate-inputs`, `day09 run`, `day09 validate` và
  `day09 package --output dist/submission.zip`.
- Thông tin bí mật được đọc từ `.env` đã bị Git bỏ qua và không bao giờ được ghi
  vào đầu ra, nhật ký, tài liệu kiến trúc hoặc gói bài nộp.
#htungf
#huytd2109