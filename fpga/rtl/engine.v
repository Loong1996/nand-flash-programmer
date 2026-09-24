// Micro-op execution engine.
//
// Consumes a byte stream of operations (see docs/protocol.md), drives the
// NAND and SPI buses and produces a byte stream of results. Operations are
// executed strictly in order; results appear in the same order.

module engine #(
    parameter        CLK_HZ           = 27_000_000,
    parameter [15:0] BAUD_DIV_DEFAULT = 16'd234,
    parameter [7:0]  RX_AW            = 8'd12,
    parameter [7:0]  GW_MAJOR         = 8'd1,
    parameter [7:0]  GW_MINOR         = 8'd0,
    parameter [7:0]  BOARD_ID         = 8'd1
) (
    input  wire        clk,
    input  wire        rst,
    // command stream (FWFT)
    input  wire        rx_valid,
    input  wire [7:0]  rx_data,
    output reg         rx_pop,
    // result stream
    input  wire        tx_full,
    output reg         tx_push,
    output reg  [7:0]  tx_data,
    input  wire        tx_drained,     // TX FIFO empty and UART idle
    // NAND
    output wire        nand_cle,
    output wire        nand_ale,
    output wire        nand_we_act,
    output wire        nand_re_act,
    output reg         nand_ce_act,
    output wire        nand_wp_hi,
    output wire [7:0]  nand_io_o,
    output wire        nand_io_oe,
    input  wire [7:0]  nand_io_i,
    input  wire        nand_rb,        // synchronized, 1 = ready
    output wire        nand_park,
    // SPI
    output reg         spi_cs_act,
    output wire        spi_sck,
    output wire        spi_mosi,
    input  wire        spi_miso,
    output wire        spi_io2_hi,
    output wire        spi_io3_hi,
    output wire        spi_park,
    // misc
    output reg  [15:0] baud_div,
    input  wire        active_port,    // 0 = UART, 1 = FT245
    input  wire [2:0]  ft_status,      // {CLKOUT active, SIWU# level, OE# level}
    input  wire        rx_overflow,    // pulse
    output wire        nand_activity,
    output wire        spi_activity,
    output wire        err_led
);
    // ------------------------------------------------------------------
    // Opcodes
    localparam OP_NOP         = 8'h00, OP_ECHO       = 8'h01, OP_INFO      = 8'h02,
               OP_SET_REG     = 8'h03, OP_DELAY_US   = 8'h04, OP_SET_BAUD  = 8'h05,
               OP_GET_PINS    = 8'h06,
               OP_NAND_CE     = 8'h10, OP_NAND_CMD   = 8'h11, OP_NAND_ADDR = 8'h12,
               OP_NAND_WRITE  = 8'h13, OP_NAND_READ  = 8'h14, OP_NAND_WAIT = 8'h15,
               OP_NAND_POLL   = 8'h16,
               OP_SPI_CS      = 8'h20, OP_SPI_WRITE  = 8'h21, OP_SPI_READ  = 8'h22,
               OP_SPI_XFER    = 8'h23, OP_SPI_POLL   = 8'h24;

    localparam integer US_DIV       = CLK_HZ / 1_000_000;
    localparam integer MS_DIV       = CLK_HZ / 1_000;
    localparam [7:0]   IB_TIMEOUT   = 8'd100;   // ms, inter-byte timeout
    localparam [15:0]  BAUD_WD_MS   = 16'd1000; // ms, baud confirmation window
    localparam [7:0]   CS_MIN_HIGH  = 8'd4;     // cycles

    // ------------------------------------------------------------------
    // Time bases
    reg [15:0] us_pre;
    reg [15:0] ms_pre;
    reg        tick_us, tick_ms;
    always @(posedge clk) begin
        tick_us <= 1'b0;
        tick_ms <= 1'b0;
        if (rst) begin
            us_pre <= 0;
            ms_pre <= 0;
        end else begin
            if (us_pre == US_DIV - 1) begin
                us_pre  <= 0;
                tick_us <= 1'b1;
            end else begin
                us_pre <= us_pre + 1'b1;
            end
            if (ms_pre == MS_DIV - 1) begin
                ms_pre  <= 0;
                tick_ms <= 1'b1;
            end else begin
                ms_pre <= ms_pre + 1'b1;
            end
        end
    end

    // ------------------------------------------------------------------
    // Configuration registers
    reg [7:0] t_setup, t_wp, t_wh, t_rp, t_reh, t_whr, t_adl, t_wb, spi_div;
    reg [7:0] pin_ctrl;

    assign nand_wp_hi = pin_ctrl[0];
    assign spi_io2_hi = pin_ctrl[1];
    assign spi_io3_hi = pin_ctrl[2];
    assign nand_park  = pin_ctrl[3];
    assign spi_park   = pin_ctrl[4];
    wire   spi_late   = pin_ctrl[5];

    // ------------------------------------------------------------------
    // Bus engines
    reg        nb_start;
    reg  [1:0] nb_kind;
    reg  [7:0] nb_wdata;
    wire [7:0] nb_rdata;
    wire       nb_busy, nb_done;

    nand_bus u_nand (
        .clk(clk), .rst(rst),
        .t_setup(t_setup), .t_wp(t_wp), .t_wh(t_wh), .t_rp(t_rp), .t_reh(t_reh),
        .t_whr(t_whr), .t_adl(t_adl),
        .start(nb_start), .kind(nb_kind), .wdata(nb_wdata), .rdata(nb_rdata),
        .busy(nb_busy), .done(nb_done),
        .cle(nand_cle), .ale(nand_ale), .we_act(nand_we_act), .re_act(nand_re_act),
        .io_o(nand_io_o), .io_oe(nand_io_oe), .io_i(nand_io_i)
    );

    reg        sp_start;
    reg  [7:0] sp_tx;
    wire [7:0] sp_rx;
    wire       sp_busy, sp_done;

    spi_master u_spi (
        .clk(clk), .rst(rst), .div(spi_div), .sample_late(spi_late),
        .start(sp_start), .tx(sp_tx), .rx(sp_rx), .busy(sp_busy), .done(sp_done),
        .sck(spi_sck), .mosi(spi_mosi), .miso(spi_miso)
    );

    assign nand_activity = nb_busy;
    assign spi_activity  = sp_busy;

    // ------------------------------------------------------------------
    // Main state machine
    localparam [5:0]
        S_FETCH    = 6'd0,  S_ARGS     = 6'd1,  S_EXEC     = 6'd2,  S_PUSH     = 6'd3,
        S_GETB     = 6'd4,  S_NAND     = 6'd5,  S_NAND_W   = 6'd6,  S_SPI      = 6'd7,
        S_SPI_W    = 6'd8,  S_INFO     = 6'd9,  S_DELAY    = 6'd10, S_BAUD_ACK = 6'd11,
        S_BAUD_SW  = 6'd12, S_PINS2    = 6'd13, S_ADDR     = 6'd14, S_NW       = 6'd15,
        S_NW2      = 6'd16, S_NR       = 6'd17, S_NR2      = 6'd18, S_WRB0     = 6'd19,
        S_WRB      = 6'd20, S_PS0      = 6'd21, S_PS       = 6'd22, S_PS2      = 6'd23,
        S_RES1     = 6'd24, S_CS_ON    = 6'd25, S_SW       = 6'd26, S_SW2      = 6'd27,
        S_SR       = 6'd28, S_SR2      = 6'd29, S_SX       = 6'd30, S_SX2      = 6'd31,
        S_SX3      = 6'd32, S_SP_CS    = 6'd33, S_SP_CMD   = 6'd34, S_SP_RD    = 6'd35,
        S_SP_CHK   = 6'd36, S_DONE     = 6'd37, S_INFO2    = 6'd38;

    reg [5:0]  st, ret;
    reg [7:0]  op;
    reg [7:0]  args [0:15];
    reg [3:0]  argc, argn;
    reg [15:0] len;
    reg [3:0]  idx;
    reg [7:0]  b;              // byte from S_GETB
    reg [7:0]  ib_ms;          // inter-byte timer
    reg [15:0] tmo_ms;         // op timeout timer
    reg [7:0]  wcnt;           // small wait counter
    reg [7:0]  cs_gap;
    reg [7:0]  last;           // last polled status
    reg [7:0]  flags;
    reg        baud_pending;
    reg [15:0] wd_ms;
    reg        result;

    assign err_led = flags[0] | flags[1] | flags[2];

    wire [15:0] arg_u16_0 = {args[1], args[0]};   // little-endian u16 in args[0..1]
    wire [7:0]  poll_n    = args[0];
    wire [7:0]  poll_mask = args[poll_n + 1];
    wire [7:0]  poll_val  = args[poll_n + 2];
    wire [15:0] poll_tmo  = {args[poll_n + 4], args[poll_n + 3]};

    function [7:0] info_byte;
        input [3:0] i;
        begin
            case (i)
                4'd0:  info_byte = 8'h4E;          // 'N'
                4'd1:  info_byte = 8'h53;          // 'S'
                4'd2:  info_byte = 8'h50;          // 'P'
                4'd3:  info_byte = 8'h47;          // 'G'
                4'd4:  info_byte = 8'd1;           // protocol version
                4'd5:  info_byte = GW_MAJOR;
                4'd6:  info_byte = GW_MINOR;
                4'd7:  info_byte = BOARD_ID;
                4'd8:  info_byte = CLK_HZ[7:0];
                4'd9:  info_byte = CLK_HZ[15:8];
                4'd10: info_byte = CLK_HZ[23:16];
                4'd11: info_byte = CLK_HZ[31:24];
                4'd12: info_byte = RX_AW;
                4'd13: info_byte = 8'h1B;          // caps: NAND8 | SPI | FT245 | UART
                4'd14: info_byte = flags;
                default: info_byte = {7'd0, active_port};
            endcase
        end
    endfunction

    // Number of argument bytes that follow each opcode (before variable parts).
    function [3:0] fixed_args;
        input [7:0] o;
        begin
            case (o)
                OP_ECHO:       fixed_args = 4'd1;
                OP_SET_REG:    fixed_args = 4'd3;
                OP_DELAY_US:   fixed_args = 4'd2;
                OP_SET_BAUD:   fixed_args = 4'd2;
                OP_NAND_CE:    fixed_args = 4'd1;
                OP_NAND_CMD:   fixed_args = 4'd1;
                OP_NAND_ADDR:  fixed_args = 4'd1;
                OP_NAND_WRITE: fixed_args = 4'd2;
                OP_NAND_READ:  fixed_args = 4'd2;
                OP_NAND_WAIT:  fixed_args = 4'd2;
                OP_NAND_POLL:  fixed_args = 4'd4;
                OP_SPI_CS:     fixed_args = 4'd1;
                OP_SPI_WRITE:  fixed_args = 4'd2;
                OP_SPI_READ:   fixed_args = 4'd2;
                OP_SPI_XFER:   fixed_args = 4'd2;
                OP_SPI_POLL:   fixed_args = 4'd1;
                default:       fixed_args = 4'd0;
            endcase
        end
    endfunction

    function known_op;
        input [7:0] o;
        begin
            case (o)
                OP_NOP, OP_ECHO, OP_INFO, OP_SET_REG, OP_DELAY_US, OP_SET_BAUD, OP_GET_PINS,
                OP_NAND_CE, OP_NAND_CMD, OP_NAND_ADDR, OP_NAND_WRITE, OP_NAND_READ,
                OP_NAND_WAIT, OP_NAND_POLL,
                OP_SPI_CS, OP_SPI_WRITE, OP_SPI_READ, OP_SPI_XFER, OP_SPI_POLL:
                    known_op = 1'b1;
                default:
                    known_op = 1'b0;
            endcase
        end
    endfunction

    wire waiting_input = (st == S_ARGS) || (st == S_GETB);

    always @(posedge clk) begin
        rx_pop   <= 1'b0;
        tx_push  <= 1'b0;
        nb_start <= 1'b0;
        sp_start <= 1'b0;

        if (cs_gap != 0 && !spi_cs_act)
            cs_gap <= cs_gap - 1'b1;

        if (tick_ms && tmo_ms != 16'hFFFF)
            tmo_ms <= tmo_ms + 1'b1;

        if (rx_overflow)
            flags[2] <= 1'b1;

        if (rst) begin
            st           <= S_FETCH;
            t_setup      <= 8'd2;
            t_wp         <= 8'd3;
            t_wh         <= 8'd2;
            t_rp         <= 8'd3;
            t_reh        <= 8'd2;
            t_whr        <= 8'd6;
            t_adl        <= 8'd8;
            t_wb         <= 8'd6;
            spi_div      <= 8'd3;
            pin_ctrl     <= 8'b0000_0110;
            nand_ce_act  <= 1'b0;
            spi_cs_act   <= 1'b0;
            cs_gap       <= 8'd0;
            flags        <= 8'd0;
            baud_div     <= BAUD_DIV_DEFAULT;
            baud_pending <= 1'b0;
            wd_ms        <= 16'd0;
            ib_ms        <= 8'd0;
            tmo_ms       <= 16'd0;
        end else begin
            // Baud-rate confirmation watchdog.
            if (baud_pending) begin
                if (tick_ms)
                    wd_ms <= wd_ms + 1'b1;
                if (wd_ms >= BAUD_WD_MS) begin
                    baud_div     <= BAUD_DIV_DEFAULT;
                    baud_pending <= 1'b0;
                    flags[3]     <= 1'b1;
                    st           <= S_FETCH;     // drop any half-received op
                end
            end

            // Inter-byte timeout while an operation is waiting for its bytes.
            if (waiting_input && !rx_valid) begin
                if (tick_ms)
                    ib_ms <= ib_ms + 1'b1;
            end else begin
                ib_ms <= 8'd0;
            end

            if (waiting_input && ib_ms >= IB_TIMEOUT) begin
                flags[1]    <= 1'b1;
                nand_ce_act <= 1'b0;
                spi_cs_act  <= 1'b0;
                cs_gap      <= CS_MIN_HIGH;
                ib_ms       <= 8'd0;
                st          <= S_FETCH;
            end else begin
                case (st)
                // ---------------------------------------------------- dispatch
                S_FETCH: begin
                    if (rx_valid && !rx_pop) begin
                        rx_pop <= 1'b1;
                        op     <= rx_data;
                        argn   <= 4'd0;
                        argc   <= fixed_args(rx_data);
                        if (!known_op(rx_data)) begin
                            flags[0] <= 1'b1;
                            st       <= S_FETCH;
                        end else if (fixed_args(rx_data) != 0) begin
                            st <= S_ARGS;
                        end else begin
                            st <= S_EXEC;
                        end
                    end
                end

                S_ARGS: begin
                    if (rx_valid && !rx_pop) begin
                        rx_pop     <= 1'b1;
                        args[argn] <= rx_data;
                        argn       <= argn + 1'b1;
                        if (argn == 4'd0 && op == OP_NAND_ADDR) begin
                            if (rx_data == 8'd0 || rx_data > 8'd8) begin
                                flags[0] <= 1'b1;
                                st       <= S_FETCH;
                            end else begin
                                argc <= 4'd1 + rx_data[3:0];
                            end
                        end else if (argn == 4'd0 && op == OP_SPI_POLL) begin
                            if (rx_data == 8'd0 || rx_data > 8'd4) begin
                                flags[0] <= 1'b1;
                                st       <= S_FETCH;
                            end else begin
                                argc <= 4'd5 + rx_data[3:0];
                            end
                        end else if (argn + 1'b1 == argc) begin
                            st <= S_EXEC;
                        end
                    end
                end

                S_EXEC: begin
                    len    <= arg_u16_0;
                    idx    <= 4'd0;
                    tmo_ms <= 16'd0;
                    case (op)
                        OP_NOP: st <= S_DONE;
                        OP_ECHO: begin
                            tx_data <= args[0];
                            ret     <= S_DONE;
                            st      <= S_PUSH;
                        end
                        OP_INFO: st <= S_INFO;
                        OP_SET_REG: begin
                            case (args[0])
                                8'd0: t_setup  <= args[1];
                                8'd1: t_wp     <= args[1];
                                8'd2: t_wh     <= args[1];
                                8'd3: t_rp     <= args[1];
                                8'd4: t_reh    <= args[1];
                                8'd5: t_whr    <= args[1];
                                8'd6: t_adl    <= args[1];
                                8'd7: t_wb     <= args[1];
                                8'd8: spi_div  <= args[1];
                                8'd9: pin_ctrl <= args[1];
                                default: flags[0] <= 1'b1;
                            endcase
                            st <= S_DONE;
                        end
                        OP_DELAY_US: st <= S_DELAY;
                        OP_SET_BAUD: begin
                            wcnt    <= 8'd16;
                            tx_data <= 8'h55;
                            ret     <= S_BAUD_SW;
                            st      <= S_PUSH;
                        end
                        OP_GET_PINS: begin
                            tx_data <= {1'b0, ft_status, baud_pending, active_port, spi_miso, nand_rb};
                            ret     <= S_PINS2;
                            st      <= S_PUSH;
                        end
                        OP_NAND_CE: begin
                            nand_ce_act <= args[0][0];
                            st          <= S_DONE;
                        end
                        OP_NAND_CMD: begin
                            nb_kind  <= 2'd0;
                            nb_wdata <= args[0];
                            ret      <= S_DONE;
                            st       <= S_NAND;
                        end
                        OP_NAND_ADDR:  st <= S_ADDR;
                        OP_NAND_WRITE: st <= S_NW;
                        OP_NAND_READ:  st <= S_NR;
                        OP_NAND_WAIT: begin
                            wcnt <= t_wb;
                            st   <= S_WRB0;
                        end
                        OP_NAND_POLL: begin
                            nb_kind  <= 2'd0;
                            nb_wdata <= 8'h70;
                            ret      <= S_PS;
                            st       <= S_NAND;
                        end
                        OP_SPI_CS: begin
                            if (args[0][0]) begin
                                st <= S_CS_ON;
                            end else begin
                                spi_cs_act <= 1'b0;
                                cs_gap     <= CS_MIN_HIGH;
                                st         <= S_DONE;
                            end
                        end
                        OP_SPI_WRITE: st <= S_SW;
                        OP_SPI_READ:  st <= S_SR;
                        OP_SPI_XFER:  st <= S_SX;
                        OP_SPI_POLL:  st <= S_SP_CS;
                        default:      st <= S_DONE;
                    endcase
                end

                S_DONE: begin
                    if (baud_pending && (op == OP_ECHO || op == OP_INFO)) begin
                        baud_pending <= 1'b0;
                        wd_ms        <= 16'd0;
                    end
                    st <= S_FETCH;
                end

                // ------------------------------------------------ subroutines
                S_PUSH: begin
                    if (!tx_full) begin
                        tx_push <= 1'b1;
                        st      <= ret;
                    end
                end
                S_GETB: begin
                    if (rx_valid && !rx_pop) begin
                        rx_pop <= 1'b1;
                        b      <= rx_data;
                        st     <= ret;
                    end
                end
                S_NAND: begin
                    nb_start <= 1'b1;
                    st       <= S_NAND_W;
                end
                S_NAND_W: begin
                    if (nb_done)
                        st <= ret;
                end
                S_SPI: begin
                    sp_start <= 1'b1;
                    st       <= S_SPI_W;
                end
                S_SPI_W: begin
                    if (sp_done)
                        st <= ret;
                end

                // ------------------------------------------------- misc ops
                S_INFO: begin
                    tx_data <= info_byte(idx);
                    ret     <= S_INFO2;
                    st      <= S_PUSH;
                end
                S_INFO2: begin
                    if (idx == 4'd15) begin
                        flags <= 8'd0;
                        st    <= S_DONE;
                    end else begin
                        idx <= idx + 1'b1;
                        st  <= S_INFO;
                    end
                end
                S_DELAY: begin
                    if (len == 0)
                        st <= S_DONE;
                    else if (tick_us)
                        len <= len - 1'b1;
                end
                S_BAUD_SW: begin
                    // Wait until the acknowledge byte has left at the old rate.
                    if (wcnt != 0) begin
                        wcnt <= wcnt - 1'b1;
                    end else if (tx_drained) begin
                        baud_div     <= (arg_u16_0 < 16'd4) ? 16'd4 : arg_u16_0;
                        baud_pending <= 1'b1;
                        wd_ms        <= 16'd0;
                        st           <= S_FETCH;
                    end
                end
                S_PINS2: begin
                    tx_data <= nand_io_i;
                    ret     <= S_DONE;
                    st      <= S_PUSH;
                end

                // ------------------------------------------------- NAND ops
                S_ADDR: begin
                    if (idx == args[0][3:0]) begin
                        st <= S_DONE;
                    end else begin
                        nb_kind  <= 2'd1;
                        nb_wdata <= args[idx + 1'b1];
                        idx      <= idx + 1'b1;
                        ret      <= S_ADDR;
                        st       <= S_NAND;
                    end
                end
                S_NW: begin
                    if (len == 0) begin
                        st <= S_DONE;
                    end else begin
                        ret <= S_NW2;
                        st  <= S_GETB;
                    end
                end
                S_NW2: begin
                    nb_kind  <= 2'd2;
                    nb_wdata <= b;
                    len      <= len - 1'b1;
                    ret      <= S_NW;
                    st       <= S_NAND;
                end
                S_NR: begin
                    if (len == 0) begin
                        st <= S_DONE;
                    end else begin
                        nb_kind <= 2'd3;
                        ret     <= S_NR2;
                        st      <= S_NAND;
                    end
                end
                S_NR2: begin
                    tx_data <= nb_rdata;
                    len     <= len - 1'b1;
                    ret     <= S_NR;
                    st      <= S_PUSH;
                end
                S_WRB0: begin
                    if (wcnt == 0) begin
                        tmo_ms <= 16'd0;
                        st     <= S_WRB;
                    end else begin
                        wcnt <= wcnt - 1'b1;
                    end
                end
                S_WRB: begin
                    if (nand_rb) begin
                        tx_data <= 8'd0;
                        ret     <= S_DONE;
                        st      <= S_PUSH;
                    end else if (tmo_ms >= arg_u16_0) begin
                        tx_data <= 8'd1;
                        ret     <= S_DONE;
                        st      <= S_PUSH;
                    end
                end
                S_PS: begin
                    nb_kind <= 2'd3;
                    ret     <= S_PS2;
                    st      <= S_NAND;
                end
                S_PS2: begin
                    last <= nb_rdata;
                    if ((nb_rdata & args[0]) == args[1]) begin
                        tx_data <= 8'd0;
                        ret     <= S_RES1;
                        st      <= S_PUSH;
                    end else if (tmo_ms >= {args[3], args[2]}) begin
                        tx_data <= 8'd1;
                        ret     <= S_RES1;
                        st      <= S_PUSH;
                    end else begin
                        st <= S_PS;
                    end
                end
                S_RES1: begin
                    tx_data <= last;
                    ret     <= S_DONE;
                    st      <= S_PUSH;
                end

                // -------------------------------------------------- SPI ops
                S_CS_ON: begin
                    if (cs_gap == 0) begin
                        spi_cs_act <= 1'b1;
                        st         <= S_DONE;
                    end
                end
                S_SW: begin
                    if (len == 0) begin
                        st <= S_DONE;
                    end else begin
                        ret <= S_SW2;
                        st  <= S_GETB;
                    end
                end
                S_SW2: begin
                    sp_tx <= b;
                    len   <= len - 1'b1;
                    ret   <= S_SW;
                    st    <= S_SPI;
                end
                S_SR: begin
                    if (len == 0) begin
                        st <= S_DONE;
                    end else begin
                        sp_tx <= 8'hFF;
                        ret   <= S_SR2;
                        st    <= S_SPI;
                    end
                end
                S_SR2: begin
                    tx_data <= sp_rx;
                    len     <= len - 1'b1;
                    ret     <= S_SR;
                    st      <= S_PUSH;
                end
                S_SX: begin
                    if (len == 0) begin
                        st <= S_DONE;
                    end else begin
                        ret <= S_SX2;
                        st  <= S_GETB;
                    end
                end
                S_SX2: begin
                    sp_tx <= b;
                    ret   <= S_SX3;
                    st    <= S_SPI;
                end
                S_SX3: begin
                    tx_data <= sp_rx;
                    len     <= len - 1'b1;
                    ret     <= S_SX;
                    st      <= S_PUSH;
                end
                S_SP_CS: begin
                    if (cs_gap == 0) begin
                        spi_cs_act <= 1'b1;
                        idx        <= 4'd0;
                        st         <= S_SP_CMD;
                    end
                end
                S_SP_CMD: begin
                    if (idx == poll_n[3:0]) begin
                        sp_tx <= 8'hFF;
                        ret   <= S_SP_RD;
                        st    <= S_SPI;
                    end else begin
                        sp_tx <= args[idx + 1'b1];
                        idx   <= idx + 1'b1;
                        ret   <= S_SP_CMD;
                        st    <= S_SPI;
                    end
                end
                S_SP_RD: begin
                    spi_cs_act <= 1'b0;
                    cs_gap     <= CS_MIN_HIGH;
                    last       <= sp_rx;
                    st         <= S_SP_CHK;
                end
                S_SP_CHK: begin
                    if ((last & poll_mask) == poll_val) begin
                        tx_data <= 8'd0;
                        ret     <= S_RES1;
                        st      <= S_PUSH;
                    end else if (tmo_ms >= poll_tmo) begin
                        tx_data <= 8'd1;
                        ret     <= S_RES1;
                        st      <= S_PUSH;
                    end else begin
                        st <= S_SP_CS;
                    end
                end

                default: st <= S_FETCH;
                endcase
            end
        end
    end
endmodule
