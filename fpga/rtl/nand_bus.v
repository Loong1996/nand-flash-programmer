// Asynchronous (ONFI SDR) NAND bus cycle generator.
//
// kind 0 = command latch, 1 = address latch, 2 = data write, 3 = data read.
// All timing inputs are in clock cycles; a value of 0 behaves like 1.
//
// Write cycle:  [gap] CLE/ALE/data set -> t_setup -> WE# low t_wp -> WE# high t_wh
// Read cycle:   [gap] RE# low t_rp (sample on last cycle) -> RE# high t_reh
// Gaps: a read that follows a write waits until t_whr cycles have passed since
// the last WE# rising edge; a data write that follows an address cycle waits
// t_adl cycles since that edge.

module nand_bus (
    input  wire       clk,
    input  wire       rst,
    input  wire [7:0] t_setup,
    input  wire [7:0] t_wp,
    input  wire [7:0] t_wh,
    input  wire [7:0] t_rp,
    input  wire [7:0] t_reh,
    input  wire [7:0] t_whr,
    input  wire [7:0] t_adl,
    input  wire       start,
    input  wire [1:0] kind,
    input  wire [7:0] wdata,
    output reg  [7:0] rdata,
    output reg        busy,
    output reg        done,
    output reg        cle,
    output reg        ale,
    output reg        we_act,
    output reg        re_act,
    output reg  [7:0] io_o,
    output reg        io_oe,
    input  wire [7:0] io_i
);
    localparam K_CMD = 2'd0, K_ADDR = 2'd1, K_WDATA = 2'd2, K_RDATA = 2'd3;
    localparam S_IDLE = 3'd0, S_GAP = 3'd1, S_SETUP = 3'd2, S_WP = 3'd3,
               S_WH = 3'd4, S_RP = 3'd5, S_REH = 3'd6;

    reg [2:0] st;
    reg [7:0] cnt;
    reg [1:0] k;
    reg [7:0] d;
    reg [7:0] since_we;      // cycles since last WE# rising edge (saturating)
    reg [1:0] last_kind;
    reg       have_last;

    function [7:0] atleast1;
        input [7:0] v;
        atleast1 = (v == 8'd0) ? 8'd1 : v;
    endfunction

    // Required gap before the pending cycle, in cycles since last WE# rise.
    wire [7:0] need_gap =
        (!have_last)                                   ? 8'd0 :
        (k == K_RDATA && last_kind != K_RDATA)         ? t_whr :
        (k == K_WDATA && last_kind == K_ADDR)          ? t_adl : 8'd0;

    always @(posedge clk) begin
        done <= 1'b0;
        if (we_act || since_we == 8'hFF)
            since_we <= since_we;
        else
            since_we <= since_we + 1'b1;

        if (rst) begin
            st        <= S_IDLE;
            busy      <= 1'b0;
            cle       <= 1'b0;
            ale       <= 1'b0;
            we_act    <= 1'b0;
            re_act    <= 1'b0;
            io_oe     <= 1'b0;
            since_we  <= 8'hFF;
            have_last <= 1'b0;
            last_kind <= K_CMD;
        end else begin
            case (st)
                S_IDLE: begin
                    if (start) begin
                        busy <= 1'b1;
                        k    <= kind;
                        d    <= wdata;
                        st   <= S_GAP;
                    end
                end
                S_GAP: begin
                    if (since_we >= need_gap) begin
                        if (k == K_RDATA) begin
                            io_oe  <= 1'b0;
                            re_act <= 1'b1;
                            cnt    <= atleast1(t_rp) - 1'b1;
                            st     <= S_RP;
                        end else begin
                            cle   <= (k == K_CMD);
                            ale   <= (k == K_ADDR);
                            io_o  <= d;
                            io_oe <= 1'b1;
                            cnt   <= atleast1(t_setup) - 1'b1;
                            st    <= S_SETUP;
                        end
                    end
                end
                S_SETUP: begin
                    if (cnt == 0) begin
                        we_act <= 1'b1;
                        cnt    <= atleast1(t_wp) - 1'b1;
                        st     <= S_WP;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                S_WP: begin
                    if (cnt == 0) begin
                        we_act   <= 1'b0;
                        since_we <= 8'd0;
                        cnt      <= atleast1(t_wh) - 1'b1;
                        st       <= S_WH;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                S_WH: begin
                    if (cnt == 0) begin
                        cle       <= 1'b0;
                        ale       <= 1'b0;
                        io_oe     <= 1'b0;
                        last_kind <= k;
                        have_last <= 1'b1;
                        busy      <= 1'b0;
                        done      <= 1'b1;
                        st        <= S_IDLE;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                S_RP: begin
                    if (cnt == 0) begin
                        rdata  <= io_i;
                        re_act <= 1'b0;
                        cnt    <= atleast1(t_reh) - 1'b1;
                        st     <= S_REH;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
                default: begin // S_REH
                    if (cnt == 0) begin
                        last_kind <= K_RDATA;
                        have_last <= 1'b1;
                        busy      <= 1'b0;
                        done      <= 1'b1;
                        st        <= S_IDLE;
                    end else begin
                        cnt <= cnt - 1'b1;
                    end
                end
            endcase
        end
    end
endmodule
