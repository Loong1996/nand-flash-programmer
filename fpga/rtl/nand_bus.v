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
//
// Handshake: the client holds `req` (with `kind`/`wdata`) until it sees the
// one-cycle `ack`. A request is taken when the bus is idle or in the last
// cycle of the previous bus cycle, so a client that keeps `req` asserted gets
// back-to-back cycles with no idle clock in between (burst reads/writes).
// Every cycle is at least two clocks long (RE#/WE# low + high), which leaves the client one clock
// after `ack` to present the next request or drop `req`.
// Read data comes out with a one-cycle `rvalid` strobe.

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
    input  wire       req,
    input  wire [1:0] kind,
    input  wire [7:0] wdata,
    output reg        ack,
    output reg  [7:0] rdata,
    output reg        rvalid,
    output reg        busy,
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

    // Last clock of the current bus cycle.
    wire last_clk = (cnt == 0) && (st == S_WH || st == S_REH);
    wire take     = req && !ack && (st == S_IDLE || last_clk);

    // Kind of the most recent bus cycle as seen by a request taken now.
    wire       hl = have_last || last_clk;
    wire [1:0] lk = last_clk ? k : last_kind;

    function [7:0] gap_for;
        input [1:0] nk;
        gap_for = (!hl)                        ? 8'd0 :
                  (nk == K_RDATA && lk != K_RDATA) ? t_whr :
                  (nk == K_WDATA && lk == K_ADDR)  ? t_adl : 8'd0;
    endfunction

    // Start driving a cycle of kind kk with data dd.
    task launch;
        input [1:0] kk;
        input [7:0] dd;
        begin
            if (kk == K_RDATA) begin
                io_oe  <= 1'b0;
                cle    <= 1'b0;
                ale    <= 1'b0;
                re_act <= 1'b1;
                cnt    <= atleast1(t_rp) - 1'b1;
                st     <= S_RP;
            end else begin
                cle   <= (kk == K_CMD);
                ale   <= (kk == K_ADDR);
                io_o  <= dd;
                io_oe <= 1'b1;
                cnt   <= atleast1(t_setup) - 1'b1;
                st    <= S_SETUP;
            end
        end
    endtask

    always @(posedge clk) begin
        ack    <= 1'b0;
        rvalid <= 1'b0;
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
            // Finishing a cycle: remember its kind for the gap rules.
            if (last_clk) begin
                last_kind <= k;
                have_last <= 1'b1;
            end

            if (take) begin
                ack  <= 1'b1;
                busy <= 1'b1;
                k    <= kind;
                d    <= wdata;
                if (since_we >= gap_for(kind)) begin
                    launch(kind, wdata);
                end else begin
                    cle   <= 1'b0;
                    ale   <= 1'b0;
                    io_oe <= 1'b0;
                    st    <= S_GAP;
                end
            end else begin
                case (st)
                    S_IDLE: ;
                    S_GAP: begin
                        if (since_we >= gap_for(k))
                            launch(k, d);
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
                            cle   <= 1'b0;
                            ale   <= 1'b0;
                            io_oe <= 1'b0;
                            busy  <= 1'b0;
                            st    <= S_IDLE;
                        end else begin
                            cnt <= cnt - 1'b1;
                        end
                    end
                    S_RP: begin
                        if (cnt == 0) begin
                            rdata  <= io_i;
                            rvalid <= 1'b1;
                            re_act <= 1'b0;
                            cnt    <= atleast1(t_reh) - 1'b1;
                            st     <= S_REH;
                        end else begin
                            cnt <= cnt - 1'b1;
                        end
                    end
                    default: begin // S_REH
                        if (cnt == 0) begin
                            busy <= 1'b0;
                            st   <= S_IDLE;
                        end else begin
                            cnt <= cnt - 1'b1;
                        end
                    end
                endcase
            end
        end
    end
endmodule
