# Clock constraints for nextpnr (fpga/build.py passes this with --sdc).
# Net names as they appear after synthesis (top_tangnano9k.v: clk = rPLL
# output from clk27, fclk_raw = ft_clkout, fclk = phase-shifted fclk_raw).
create_clock -name clk27 -period 37.037 [get_nets {clk27}]
create_clock -name clk -period 18.518 [get_nets {clk}]
create_clock -name fclk_raw -period 16.667 [get_nets {fclk_raw}]
create_clock -name fclk -period 16.667 [get_nets {fclk}]
