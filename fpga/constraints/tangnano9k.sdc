# Clock constraints for nextpnr (fpga/build.py passes this with --sdc).
# Net names as they appear after synthesis (top_tangnano9k.v: clk = clk27,
# fclk = ft_clkout).
create_clock -name clk -period 37.037 [get_nets {clk}]
create_clock -name fclk -period 16.667 [get_nets {fclk}]
